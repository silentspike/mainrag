"""Administrator-owned local WAL observation with narrow application authority."""
from eval.storage_v2.schema.test_content_schema import ContentSchemaFixture, ROOT

MIGRATION = ROOT / 'migrations/126_storage_v2_local_wal_backpressure.sql'
SIGNATURE = 'public.storage_v2_local_wal_ready_bytes()'


class LocalWalBackpressureTests(ContentSchemaFixture):
    def test_local_byte_count_and_authority(self):
        self.file(MIGRATION)
        self.file(MIGRATION)
        expected = self.sql("SELECT count(*)::bigint * pg_size_bytes(current_setting('wal_segment_size')) "
                            "FROM pg_ls_archive_statusdir() WHERE name LIKE '%.ready'")
        self.assertEqual(self.sql(f'SET ROLE mainrag; SELECT {SIGNATURE}'), expected)
        self.assert_sql_fails('SET ROLE mainrag; SELECT * FROM pg_ls_archive_statusdir()', 'permission denied')
        self.assert_sql_fails(f'SET ROLE storage_v2_pack_worker; SELECT {SIGNATURE}', 'permission denied')
        self.assert_sql_fails('SET ROLE mainrag; ' + MIGRATION.read_text(),
                              'requires the database administrator')

    def test_observer_drift_is_not_repaired_implicitly(self):
        self.file(MIGRATION)
        body = MIGRATION.read_text().replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
        definition = self.sql(f"SELECT pg_get_functiondef('{SIGNATURE}'::regprocedure)")
        for mutation, error in (
            (definition.replace('count(*)::bigint', '(count(*)+1)::bigint') + ';', 'definition'),
            (f'ALTER FUNCTION {SIGNATURE} SET row_security=off;', 'definition'),
            (f'ALTER FUNCTION {SIGNATURE} OWNER TO mainrag;', 'definition'),
            (f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO PUBLIC;', 'authority'),
            (f'GRANT EXECUTE ON FUNCTION {SIGNATURE} TO storage_v2_pack_worker;', 'authority'),
            (f'REVOKE EXECUTE ON FUNCTION {SIGNATURE} FROM mainrag;', 'authority'),
        ):
            self.assert_sql_fails('BEGIN;' + mutation + body + 'ROLLBACK;',
                                  f'local WAL observer {error} differs')
