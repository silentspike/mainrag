"""Complete v1 export equivalence from ordered PostgreSQL record streams."""
import hashlib
import json
import unittest

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_intelligence_export_text as previous
from eval.storage_v2.schema import test_structural_card_reuse as cards

MIGRATION = schema.ROOT / 'migrations/119_storage_v2_streamed_intelligence_records.sql'
COLLECTIONS = sorted(('profiles', 'cards', 'annotations', 'entities', 'relations',
                      'call_edges', 'unresolved_calls', 'negative_evidence'),
                     key=lambda value: (len(value), value))


class StreamedIntelligenceRecordsTests(unittest.TestCase):
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
    card_fixture = previous.IntelligenceExportTextTests.card_fixture
    export = previous.IntelligenceExportTextTests.export

    @classmethod
    def setUpClass(cls):
        try:
            schema.ShadowIngestSchemaTests.setUpClass.__func__(cls)
            cls.file(previous.MIGRATION)
            cls.sql("""
DO $$ DECLARE relation REGCLASS; routine REGPROCEDURE; BEGIN
 FOR relation IN SELECT oid::REGCLASS FROM pg_class
  WHERE relnamespace='public'::regnamespace AND relkind IN ('r','p')
    AND relowner=current_user::regrole LOOP
  EXECUTE format('ALTER TABLE %s OWNER TO mainrag',relation);
 END LOOP;
 FOR routine IN SELECT oid::REGPROCEDURE FROM pg_proc
  WHERE pronamespace='public'::regnamespace AND proowner=current_user::regrole
    AND (proname LIKE 'storage_v2_%' OR proname='user_can_access_source') LOOP
  EXECUTE format('ALTER FUNCTION %s OWNER TO mainrag',routine);
 END LOOP;
END $$;
ALTER FUNCTION storage_v2_export_intelligence(bigint,text,text) OWNER TO mainrag;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mainrag;
""")
            cls.file(MIGRATION)
        except BaseException:
            if hasattr(cls, "stack"):
                cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        schema.ShadowIngestSchemaTests.tearDownClass.__func__(cls)

    def streamed(self, source, generation):
        values = 'ARRAY[' + ','.join(cards.literal(name) for name in COLLECTIONS) + ']::TEXT[]'
        prefix = f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
        raw = self.sql(prefix + f"""
SELECT jsonb_build_array(collection.ordinal,record.record_ordinal,record.record_text)::TEXT
FROM unnest({values}) WITH ORDINALITY collection(name,ordinal)
LEFT JOIN LATERAL storage_v2_intelligence_export_records(
    {source},{cards.literal(generation)},collection.name) record ON TRUE;
""")
        counts = {name: 0 for name in COLLECTIONS}
        data = {}
        previous_collection = 0
        for line in raw.splitlines():
            collection, ordinal, record = json.loads(line)
            self.assertIn(collection, (previous_collection, previous_collection + 1))
            previous_collection = collection
            name = COLLECTIONS[collection - 1]
            if ordinal is None:
                self.assertIsNone(record)
                self.assertNotIn(name, data)
                data[name] = []
            else:
                self.assertEqual(ordinal, counts[name] + 1)
                counts[name] += 1
                data.setdefault(name, []).append(record)
        self.assertEqual(previous_collection, len(COLLECTIONS))
        payload = '{' + ', '.join(json.dumps(name) + ': [' + ', '.join(data[name]) + ']'
                                  for name in COLLECTIONS) + '}'
        return counts, hashlib.sha256(payload.encode()).hexdigest(), payload

    def assert_equivalent(self, source, generation):
        protected = json.loads(self.export(source, generation, 'protected'))
        public = json.loads(self.export(source, generation, 'public'))
        counts, digest, payload = self.streamed(source, generation)
        self.assertEqual(digest, protected['payload_sha256'])
        self.assertEqual(digest, public['payload']['protected_payload_sha256'])
        self.assertEqual(counts, public['payload']['record_counts'])
        self.assertEqual(json.loads(payload), protected['payload'])
        return counts

    def test_all_legacy_classes_generation_scope_and_import_round_trip(self):
        schema.ShadowIngestSchemaTests.test_intelligence_provenance_retry_and_round_trip(self)
        for source, generation in ((4, '1'), (4, '2'), (5, '1')):
            counts = self.assert_equivalent(source, generation)
            if source == 4 and generation == '1':
                self.assertTrue(all(value > 0 for value in counts.values()))

    def test_unicode_numeric_escaping_and_large_ordered_collection(self):
        args = self.card_fixture(2500)
        counts = self.assert_equivalent(args['p_source_id'], '1')
        self.assertEqual(counts['cards'], 2500)
        self.assertTrue(all(value == 0 for key, value in counts.items() if key != 'cards'))

    def test_repeated_order_keys_keep_every_record(self):
        args = self.card_fixture(2)
        args['p_symbol_key'] = "'fixture-export-1'"
        for variant in range(1, 9):
            args['p_structure'] = f"jsonb_build_object('kind','function','variant',{variant})"
            self.sql(self.admin(cards.StructuralCardReuseTests.call(args)))
        counts = self.assert_equivalent(args['p_source_id'], '1')
        self.assertEqual(counts['cards'], 10)

    def test_authority_invalid_collection_and_reapply_preserve_semantics(self):
        args = self.card_fixture()
        source = args['p_source_id']
        state = cards.StructuralCardReuseTests.data_identity(self)
        before = self.export(source, '1', 'protected')
        for _ in range(2):
            self.file(MIGRATION)
            self.assert_equivalent(source, '1')
            self.assertEqual(self.export(source, '1', 'protected'), before)
            self.assertEqual(cards.StructuralCardReuseTests.data_identity(self), state)
        signature = 'storage_v2_intelligence_export_records(bigint,text,text)'
        self.assertEqual(self.sql(f"SELECT has_function_privilege('storage_v2_shadow_worker','{signature}','EXECUTE')"), 'f')
        prefix = f"SET ROLE mainrag; SET app.user_id='{schema.ADMIN_ID}'; "
        for name in ("'unknown'", 'NULL'):
            self.assert_sql_fails(prefix + f"SELECT * FROM storage_v2_intelligence_export_records({source},'1',{name})", 'known intelligence export collection')
        self.assert_sql_fails(f"SET ROLE mainrag; SET app.user_id='{schema.OTHER_ID}'; "
            + f"SELECT * FROM storage_v2_intelligence_export_records({source},'1','cards')", 'authorized generation selector')

        candidate = self.sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
        migration_body = MIGRATION.read_text().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
        for change, error in (
            (candidate.replace("RETURN QUERY", "/* fixture drift */ RETURN QUERY", 1) + ";", "identity"),
            (f"ALTER FUNCTION {signature} OWNER TO storage_v2_shadow_worker;", "identity"),
            (f"ALTER FUNCTION {signature} SECURITY INVOKER;", "identity"),
            (f"GRANT EXECUTE ON FUNCTION {signature} TO storage_v2_shadow_worker;", "authority"),
        ):
            result = self.command("--command", "BEGIN; " + change + migration_body + "ROLLBACK;", check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"streamed intelligence export {error} differs", result.stderr)
