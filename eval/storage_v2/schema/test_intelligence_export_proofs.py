"""Reuse complete export proofs; reject data, function and authorization drift."""
import json
import unittest

from eval.storage_v2.schema import test_streamed_intelligence_records as streamed
from eval.storage_v2.schema import test_shadow_ingest_schema as schema

MIGRATION = schema.ROOT / 'migrations/127_storage_v2_reuse_intelligence_export_proofs.sql'


class IntelligenceExportProofTests(unittest.TestCase):
    command = streamed.StreamedIntelligenceRecordsTests.__dict__['command']
    sql = streamed.StreamedIntelligenceRecordsTests.__dict__['sql']
    file = streamed.StreamedIntelligenceRecordsTests.__dict__['file']
    admin = streamed.StreamedIntelligenceRecordsTests.__dict__['admin']
    actor = streamed.StreamedIntelligenceRecordsTests.__dict__['actor']
    make_projection = schema.ShadowIngestSchemaTests.make_projection
    begin = schema.ShadowIngestSchemaTests.begin
    stage = schema.ShadowIngestSchemaTests.stage
    complete_analysis = schema.ShadowIngestSchemaTests.complete_analysis
    commit = schema.ShadowIngestSchemaTests.commit
    assert_sql_fails = schema.ShadowIngestSchemaTests.assert_sql_fails
    export = streamed.StreamedIntelligenceRecordsTests.export

    @classmethod
    def setUpClass(cls):
        streamed.StreamedIntelligenceRecordsTests.setUpClass.__func__(cls)
        cls.file(MIGRATION)
        cls.file(MIGRATION)

    @classmethod
    def tearDownClass(cls):
        schema.ShadowIngestSchemaTests.tearDownClass.__func__(cls)

    def cache_sql(self, statement, user=schema.ADMIN_ID):
        return self.sql(f"SET ROLE mainrag; SET app.user_id='{user}'; " + statement)

    def card_fixture(self):
        result = streamed.StreamedIntelligenceRecordsTests.card_fixture(self)
        source = result['p_source_id']
        self.cache_sql('SELECT storage_v2_verify_generation('
            f"(SELECT id FROM source_generation WHERE source_id={source} AND generation_seq=1),"
            f"'{source.zfill(64)}');")
        return result

    def store(self, source):
        return self.cache_sql(f"""
SELECT storage_v2_store_intelligence_export_proof({source},'1',
    storage_v2_intelligence_export_proof_identity({source},'1'),
    storage_v2_export_intelligence({source},'1','public'),
    octet_length((storage_v2_export_intelligence({source},'1','protected')->'payload')::TEXT));
""")

    def cached(self, source):
        return json.loads(self.cache_sql(
            f"SELECT storage_v2_cached_intelligence_export_proof({source},'1');"))

    def test_reuse_and_source_local_invalidation_without_count_changes(self):
        first = self.card_fixture()
        second = self.card_fixture()
        source = first['p_source_id']
        other = second['p_source_id']
        self.assertEqual(self.store(source), 't')
        self.assertEqual(self.store(other), 't')
        cached = self.cached(source)
        self.assertEqual(cached['proof']['public_envelope'],
                         json.loads(self.export(source, '1', 'public')))
        self.assertEqual(self.cached(source), cached)
        # A card update keeps the row count but changes the exported payload.
        self.cache_sql("UPDATE storage_v2_symbol_card SET generic_card="
                       "generic_card||'{\"changed\":true}'::JSONB WHERE "
                       f"symbol_occurrence_id IN (SELECT id FROM storage_v2_symbol_occurrence "
                       f"WHERE source_id={source});")
        self.assertIsNone(self.cached(source)['proof'])
        self.assertIsNotNone(self.cached(other)['proof'])
        stale = json.dumps(cached['identity']).replace("'", "''")
        self.assertEqual(self.cache_sql(f"SELECT storage_v2_store_intelligence_export_proof("
            f"{source},'1','{stale}'::JSONB,"
            f"storage_v2_export_intelligence({source},'1','public'),1);"), 'f')
        self.assertEqual(self.store(source), 't')
        self.assertNotEqual(self.cached(source)['proof']['public_envelope'],
                            cached['proof']['public_envelope'])
        # Rollback restores both the data and its valid proof revision.
        current = self.cached(source)
        self.cache_sql("BEGIN; DELETE FROM storage_v2_symbol_card WHERE "
                       f"symbol_occurrence_id IN (SELECT id FROM storage_v2_symbol_occurrence "
                       f"WHERE source_id={source}); ROLLBACK;")
        self.assertEqual(self.cached(source), current)

    def test_exporter_change_truncate_and_unauthorized_access_invalidate(self):
        source = self.card_fixture()['p_source_id']
        self.assertEqual(self.store(source), 't')
        signature = 'storage_v2_intelligence_export_records(bigint,text,text)'
        definition = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure);")
        changed = definition.replace('BEGIN', 'BEGIN\n-- synthetic exporter identity change', 1)
        self.sql(changed)
        self.assertIsNone(self.cached(source)['proof'])
        self.sql(definition)
        self.assertIsNotNone(self.cached(source)['proof'])
        self.assert_sql_fails(f"SET ROLE mainrag; SET app.user_id='{schema.OTHER_ID}'; "
            f"SELECT storage_v2_cached_intelligence_export_proof({source},'1');", 'not authorized')
        self.assert_sql_fails('SET ROLE storage_v2_shadow_worker; '
            'SELECT * FROM storage_v2_intelligence_export_proof;', 'permission denied')
        self.sql('TRUNCATE storage_v2_symbol_card;')
        self.assertIsNone(self.cached(source)['proof'])

    def test_missing_or_incomplete_payload_cannot_be_stored(self):
        self.assert_sql_fails('SET ROLE mainrag; ' + MIGRATION.read_text(),
                              'requires the database administrator')
        source = self.card_fixture()['p_source_id']
        for transformation in (
            " #- '{payload,protected_payload_sha256}'",
            " #- '{payload,record_counts,negative_evidence}'",
        ):
            with self.subTest(transformation=transformation):
                # Recompute the public payload checksum, so rejection exercises
                # completeness rather than only a mismatched checksum.
                self.assert_sql_fails(f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
                    f"WITH changed AS (SELECT storage_v2_export_intelligence({source},'1','public')"
                    f"{transformation} value), envelope AS (SELECT value||jsonb_build_object("
                    "'payload_sha256',encode(sha256(convert_to((value->'payload')::TEXT,'UTF8')),'hex')) value FROM changed) "
                    f"SELECT storage_v2_store_intelligence_export_proof({source},'1',"
                    f"storage_v2_intelligence_export_proof_identity({source},'1'),value,1) FROM envelope;",
                    'required')
