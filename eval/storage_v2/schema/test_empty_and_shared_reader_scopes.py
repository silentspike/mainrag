"""Complete result equivalence and physical work bounds for reader scopes."""
import json
import re
from unittest.mock import patch

from eval.storage_v2.schema import test_shared_query_postings as previous

MIGRATION = previous.ROOT / "migrations/124_storage_v2_empty_and_shared_reader_scopes.sql"


class EmptyAndSharedReaderScopeTests(previous.SharedQueryPostingTests):
    @classmethod
    def setUpClass(cls):
        previous.SharedQueryPostingTests.setUpClass.__func__(cls)
        try:
            cls.file(previous.METADATA)
            cls.file(previous.QUERY)
        except BaseException:
            cls.stack.close()
            raise

    def test_a_complete_active_and_exact_envelopes(self):
        signature = "storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)"
        original = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
        # Retain an independent native reference with the same definer and RLS.
        reference = original.replace("public.storage_v2_authorized_lexical_candidates",
                                     "public.fixture_reference_lexical_candidates", 1)
        self.sql(reference + "; ALTER FUNCTION fixture_reference_lexical_candidates(bigint[],bigint[],text) "
                 "OWNER TO mainrag_v2_lexical_rank_owner; "
                 "REVOKE ALL ON FUNCTION fixture_reference_lexical_candidates(bigint[],bigint[],text) FROM PUBLIC; "
                 "GRANT EXECUTE ON FUNCTION fixture_reference_lexical_candidates(bigint[],bigint[],text) "
                 "TO mainrag_v2_frontier_owner;")
        with patch.object(previous, "QUERY", MIGRATION):
            previous.SharedQueryPostingTests.test_a_complete_active_and_exact_envelopes(self)
        requested = "ARRAY(SELECT id FROM occurrence WHERE source_id IN (6,9))"
        for user in (self.schema.ADMIN_ID, self.schema.WRITER_ID, self.schema.OTHER_ID):
            for scope in ("ARRAY[6,9]::bigint[]", "ARRAY[6,9,6,NULL]::bigint[]",
                          "ARRAY[1,2]::bigint[]", "ARRAY[]::bigint[]"):
                for query in ("alpha", "alpha beta", '"alpha beta"', "alpha OR gamma", "missing"):
                    statement = f"""
WITH actual AS (SELECT * FROM storage_v2_authorized_lexical_candidates({requested},{scope},{self.quote(query)})),
reference AS (SELECT * FROM fixture_reference_lexical_candidates({requested},{scope},{self.quote(query)}))
SELECT NOT EXISTS(SELECT * FROM actual EXCEPT ALL SELECT * FROM reference)
 AND NOT EXISTS(SELECT * FROM reference EXCEPT ALL SELECT * FROM actual)
"""
                    self.assertEqual(self.sql(f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{user}';" + statement), "t")
        print("independent native candidate comparisons: 60", flush=True)
        # An authorized source with no native rows must return before expanding
        # a large occurrence array. Inspect actual nested executor plans rather
        # than asserting a fragile wall-clock threshold.
        result = self.command("--command", "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_nested_statements=on; SET auto_explain.log_analyze=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "
            f"SET ROLE mainrag_v2_frontier_owner; SET app.user_id='{self.schema.ADMIN_ID}'; "
            "SELECT count(*) FROM storage_v2_authorized_lexical_candidates("
            "ARRAY(SELECT i::bigint FROM generate_series(1,130908) i),ARRAY[1,2]::bigint[],'alpha');")
        plans = self.plans(result.stderr)
        self.assertTrue(plans)
        self.assertFalse(any("WITH authorized_sources AS MATERIALIZED" in plan["Query Text"] for plan in plans))

    @staticmethod
    def plans(stderr):
        decoder = json.JSONDecoder()
        return [decoder.raw_decode(stderr[match.start():])[0]
                for match in re.finditer(r'\{\s*"Query Text"', stderr)]

    def test_sparse_dense_multi_term_duplicate_null_and_collision_scopes(self):
        with patch.object(previous, "QUERY", MIGRATION):
            previous.SharedQueryPostingTests.test_sparse_dense_multi_term_duplicate_null_and_collision_scopes(self)
        # A broad ordinary-only scope must not enumerate global compact term
        # matches. The independent flat reference includes the scoped term rows.
        scope = "ARRAY(SELECT i::bigint FROM generate_series(2,12000) i WHERE i%5<>0)"
        result = self.command("--command", "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_nested_statements=on; SET auto_explain.log_analyze=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "
            f"SELECT count(*) FROM query_fixture.scoped({scope},ARRAY['common']);")
        self.assertEqual(result.stdout.strip(), "9599")
        self.assertFalse(any("matching_block AS MATERIALIZED" in plan["Query Text"]
                             for plan in self.plans(result.stderr)))
        self.assertTrue(self.plans(result.stderr))
        # Dense compact scope must resolve candidate keys as a set, with no
        # compact payload/index lookup for every requested document.
        dense = self.command("--command", "LOAD 'auto_explain'; SET auto_explain.log_min_duration=0; "
            "SET auto_explain.log_nested_statements=on; SET auto_explain.log_analyze=on; "
            "SET auto_explain.log_format=json; SET auto_explain.log_level=notice; "
            "SELECT count(*) FROM query_fixture.scoped("
            "ARRAY(SELECT i::bigint FROM generate_series(1,45000) i),ARRAY['common','sparse']);")
        compact_plans = [plan for plan in self.plans(dense.stderr)
                         if "matching_block AS MATERIALIZED" in plan["Query Text"]]
        self.assertEqual(len(compact_plans), 1)
        def nodes(node):
            yield node
            for child in node.get("Plans", []):
                yield from nodes(child)
        matching = [node for node in nodes(compact_plans[0]["Plan"])
                    if node.get("Subplan Name") == "CTE matching_block"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["Actual Loops"], 1)
        self.assertFalse(any(node.get("Actual Loops", 0) > 1 for node in nodes(matching[0])))

    def test_metadata_scope_does_not_expand_unrelated_vectors(self):
        with patch.object(previous, "QUERY", MIGRATION):
            previous.SharedQueryPostingTests.test_metadata_scope_does_not_expand_unrelated_vectors(self)

    def test_function_and_index_drift_abort(self):
        self.file(MIGRATION)
        self.file(MIGRATION)
        body = MIGRATION.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        for signature in ("storage_v2_scoped_query_posting(bigint[],text[])",
                          "storage_v2_authorized_lexical_candidates(bigint[],bigint[],text)"):
            definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
            owner = self.sql(f"SELECT proowner::regrole FROM pg_proc WHERE oid='{signature}'::regprocedure")
            for change, error in (
                (definition.replace("RETURN QUERY", "/* drift */ RETURN QUERY", 1) + ";", "definition"),
                (f"GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;", "authority"),
                (f"REVOKE EXECUTE ON FUNCTION {signature} FROM {owner};", "authority"),
                (f"ALTER FUNCTION {signature} SET row_security=off;", "definition"),
                (f"ALTER FUNCTION {signature} OWNER TO storage_v2_shadow_worker;", "definition"),
            ):
                result = self.command("--command", "BEGIN;" + change + body + "ROLLBACK;", check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("reader scope " + error + " differs", result.stderr)
