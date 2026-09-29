"""Managed prefix reuse through the actual dedicated frontier owner."""

import unittest

from eval.storage_v2.schema import test_shadow_ingest_schema as schema


class ManagedFrontierLockTests(unittest.TestCase):
    command = classmethod(schema.ShadowIngestSchemaTests.command.__func__)
    sql = classmethod(schema.ShadowIngestSchemaTests.sql.__func__)
    file = classmethod(schema.ShadowIngestSchemaTests.file.__func__)
    admin = classmethod(schema.ShadowIngestSchemaTests.admin.__func__)
    actor = staticmethod(schema.ShadowIngestSchemaTests.actor)
    make_projection = schema.ShadowIngestSchemaTests.make_projection
    begin = schema.ShadowIngestSchemaTests.begin
    stage = schema.ShadowIngestSchemaTests.stage
    complete_analysis = schema.ShadowIngestSchemaTests.complete_analysis
    commit = schema.ShadowIngestSchemaTests.commit
    assert_sql_fails = schema.ShadowIngestSchemaTests.assert_sql_fails

    @classmethod
    def setUpClass(cls):
        schema.ShadowIngestSchemaTests.setUpClass.__func__(cls)
        cls.sql("""
DO $$ DECLARE relation REGCLASS; routine REGPROCEDURE; BEGIN
 FOR relation IN SELECT oid::REGCLASS FROM pg_class
  WHERE relnamespace='public'::REGNAMESPACE AND relkind IN ('r','p') LOOP
  EXECUTE format('ALTER TABLE %s OWNER TO mainrag',relation);
 END LOOP;
 FOR routine IN SELECT oid::REGPROCEDURE FROM pg_proc
  WHERE pronamespace='public'::REGNAMESPACE AND proowner=current_user::REGROLE
    AND (proname LIKE 'storage_v2_%' OR proname='user_can_access_source') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO mainrag',routine);
 END LOOP;
END $$;
""")
        cls.file(schema.ROOT / "migrations/066_storage_v2_controlled_frontier_owner.sql")
        cls.file(schema.ROOT / "migrations/067_storage_v2_active_ingest_receipt_owner.sql")

    @classmethod
    def tearDownClass(cls):
        schema.ShadowIngestSchemaTests.tearDownClass.__func__(cls)

    def runtime(self, query, user=schema.ADMIN_ID):
        return f"SET ROLE mainrag; SET app.user_id='{user}'; {query}"

    def test_complete_prefix_reuse_retains_frontier_authority(self):
        profile = "mainrag.managed-append-fixture.v2.manifest"
        epoch = "00000000-0000-4000-8000-000000000100"
        node, view, digest = self.make_projection("alpha")
        first = self.begin(28, "a1" * 32, "a2" * 32, adapter_profile=profile)
        self.stage(first, "epoch/segments/00000001-a.jsonl", "alpha",
                   node, view, digest, adapter_profile=profile)
        self.complete_analysis(digest)
        self.commit(first, 1)
        self.sql(self.admin(
            "SELECT storage_v2_verify_generation("
            f"(SELECT generation_id FROM storage_v2_ingest_run WHERE id={first}),"
            f"'{'a3' * 32}');"))
        publish = (f"SELECT storage_v2_publish_managed_append_frontier({first},NULL,"
                   f"'{epoch}',1,decode('{'a4' * 32}','hex'),TRUE);")
        self.assertEqual(self.sql(self.runtime(publish)), "1")
        second = self.begin(28, "b1" * 32, "b2" * 32, adapter_profile=profile)
        copy = (f"SELECT storage_v2_copy_managed_append_prefix({second},{first},"
                "ARRAY['epoch/segments/00000001-a.jsonl']::TEXT[],"
                f"ARRAY[decode('{digest}','hex')]::BYTEA[],ARRAY[5]::BIGINT[]);")
        self.assert_sql_fails(self.runtime(copy), "row-level security policy")
        migration = schema.ROOT / "migrations/100_storage_v2_managed_frontier_lock.sql"
        self.file(migration)
        self.file(migration)
        self.assertEqual(self.sql(self.runtime(copy)), "1")
        self.assertEqual(self.sql(self.runtime(copy)), "1")
        self.assertEqual(self.sql(
            f"SELECT parser_pass_count FROM storage_v2_ingest_run_item WHERE run_id={second}"), "0")
        self.assert_sql_fails(self.runtime(copy.replace(digest, "ff" * 32)),
                              "prior staged content is incomplete or changed")
        self.assert_sql_fails(self.runtime(copy, schema.OTHER_ID),
                              "managed append runs are not reusable")
        helper = f"SELECT count(*) FROM storage_v2_lock_managed_append_frontier(28,'{profile}');"
        self.assertEqual(self.sql(self.runtime(helper)), "1")
        self.assert_sql_fails(self.runtime(helper, schema.OTHER_ID),
                              "authorized managed append frontier required")
        self.assert_sql_fails(self.runtime(helper.replace("(28,", "(3,")),
                              "authorized managed append frontier required")
        self.assertEqual(self.sql(self.runtime(
            "SELECT count(*) FROM storage_v2_lock_managed_append_frontier(NULL,NULL);")), "0")
        self.assert_sql_fails(self.admin(helper), "permission denied for function")
        self.assert_sql_fails(self.runtime(
            "UPDATE storage_v2_managed_append_frontier SET appends_since_full=99 WHERE source_id=28;"),
            "permission denied for table storage_v2_managed_append_frontier")
        self.assertEqual(self.sql(
            "SELECT last_run_id||':'||appends_since_full FROM storage_v2_managed_append_frontier "
            "WHERE source_id=28"), f"{first}:0")
        self.assertEqual(self.sql("SELECT active_generation_id IS NULL FROM logical_source WHERE id=28"), "t")
