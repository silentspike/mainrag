"""Reader equivalence, canonical provenance and untrusted source hints."""
from unittest.mock import patch

from eval.storage_v2.schema import test_bound_native_rank_work as parent

MIGRATION = parent.schema.ROOT / "migrations/117_storage_v2_authorized_copied_rank_scope.sql"


class AuthorizedCopiedRankScopeTests(parent.BoundNativeRankTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for number in range(106, 109):
            cls.file(next((parent.schema.ROOT / "migrations").glob(f"{number:03}_*.sql")))

    def test_complete_results_authorization_ties_and_replay(self):
        # Compare all complete envelopes against the previous installed reader,
        # including compound queries, stage scores, exact ties and fragments.
        with patch.object(parent, "MIGRATION", MIGRATION):
            super().test_complete_results_authorization_ties_and_replay()

        helper = "storage_v2_source_segment_rank_candidates(bigint[],text,bigint[])"
        self.assertEqual(self.sql(f"SELECT has_function_privilege('mainrag','{helper}','EXECUTE')"), "t")
        self.assertEqual(self.sql(f"SELECT has_function_privilege('storage_v2_shadow_worker','{helper}','EXECUTE')"), "f")
        ids = "ARRAY(SELECT id FROM occurrence WHERE source_id IN (6,9))"
        # The reference has no caller-supplied source hints and independently
        # resolves actual source ownership from the requested occurrence IDs.
        for actor in (parent.schema.ADMIN_ID, parent.schema.WRITER_ID, parent.schema.OTHER_ID):
            for query in ("alpha", "common", "alpha beta", "absent"):
                for scope in (ids, ids + "||ARRAY[NULL,0]::bigint[]", ids + "||" + ids):
                    for hints in ("ARRAY[6,9]::bigint[]", "ARRAY[6,6,9,NULL,0]::bigint[]"):
                        value = self.sql(f"SET ROLE mainrag; SET app.user_id='{actor}'; " + f"""
WITH old AS (SELECT * FROM storage_v2_source_segment_rank_candidates({scope},{self.quote(query)})),
new AS (SELECT * FROM storage_v2_source_segment_rank_candidates({scope},{self.quote(query)},{hints}))
SELECT coalesce((SELECT jsonb_agg(to_jsonb(old) ORDER BY occurrence_id,score,segment_order) FROM old),'[]')
     = coalesce((SELECT jsonb_agg(to_jsonb(new) ORDER BY occurrence_id,score,segment_order) FROM new),'[]');
""")
                        self.assertEqual(value, "t", (actor, query, scope, hints))
            for hints in ("ARRAY[]::bigint[]", "ARRAY[7,0,NULL]::bigint[]", "NULL::bigint[]"):
                self.assertEqual(self.sql(f"SET ROLE mainrag; SET app.user_id='{actor}'; "
                    f"SELECT count(*) FROM storage_v2_source_segment_rank_candidates({ids},'alpha',{hints})"), "0")

        broad_ids = "ARRAY(SELECT id FROM occurrence CROSS JOIN generate_series(1,1200) WHERE source_id IN (6,9))"
        broad_comparison = f"""
WITH old AS (SELECT * FROM storage_v2_source_segment_rank_candidates({broad_ids},'alpha')),
new AS (SELECT * FROM storage_v2_source_segment_rank_candidates({broad_ids},'alpha',ARRAY[6,9]::bigint[]))
SELECT count(*)>0 AND coalesce((SELECT jsonb_agg(to_jsonb(old) ORDER BY occurrence_id,score,segment_order) FROM old),'[]')
     = coalesce((SELECT jsonb_agg(to_jsonb(new) ORDER BY occurrence_id,score,segment_order) FROM new),'[]') FROM new;
"""
        self.assertEqual(self.sql(f"SET ROLE mainrag; SET app.user_id='{parent.schema.ADMIN_ID}'; "
            + broad_comparison), "t")

        # A valid hint cannot repair an inconsistent canonical document binding.
        prefix = f"SET ROLE mainrag; SET app.user_id='{parent.schema.ADMIN_ID}'; "
        mismatch = self.sql("SELECT min(id) FROM storage_v2_search_document WHERE component_kind='node'")
        comparison = f"""
WITH old AS (SELECT * FROM storage_v2_source_segment_rank_candidates({ids},'alpha')),
new AS (SELECT * FROM storage_v2_source_segment_rank_candidates({ids},'alpha',ARRAY[6,9]::bigint[]))
SELECT coalesce((SELECT jsonb_agg(to_jsonb(old) ORDER BY occurrence_id,score,segment_order) FROM old),'[]')
     = coalesce((SELECT jsonb_agg(to_jsonb(new) ORDER BY occurrence_id,score,segment_order) FROM new),'[]');
"""
        result = self.sql("BEGIN; ALTER TABLE storage_v2_search_view_document DISABLE TRIGGER USER; "
            f"UPDATE storage_v2_search_view_document SET document_id={mismatch} WHERE ordinal=0; "
            + prefix + comparison + "ROLLBACK;")
        self.assertEqual(result, "t")

        body = MIGRATION.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        candidate = self.sql(f"SELECT pg_get_functiondef('{helper}'::regprocedure)")
        for change, error in (
            (candidate.replace("RETURN QUERY", "/* fixture drift */ RETURN QUERY", 1) + ";", "identity"),
            (f"GRANT EXECUTE ON FUNCTION {helper} TO storage_v2_shadow_worker;", "authority"),
            (f"REVOKE EXECUTE ON FUNCTION {helper} FROM mainrag;", "authority"),
            (f"ALTER FUNCTION {helper} OWNER TO mainrag;", "authority"),
        ):
            drift = self.command("--command", "BEGIN; " + change + body + "ROLLBACK;", check=False)
            self.assertNotEqual(drift.returncode, 0)
            self.assertIn(f"candidate helper {error} differs", drift.stderr)

        print("untrusted hint, duplicate and null differential cases: 72; denied hint cases: 9", flush=True)
