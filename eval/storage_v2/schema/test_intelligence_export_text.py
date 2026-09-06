"""Differential v1 export checks on an owned disposable PostgreSQL fixture."""
import json
import unittest

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_structural_card_reuse as cards


MIGRATION = schema.ROOT / 'migrations/051_storage_v2_intelligence_export_text.sql'
PREVIOUS = schema.ROOT / 'migrations/033_storage_v2_intelligence.sql'


class IntelligenceExportTextTests(schema.ShadowIngestSchemaTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        definition = PREVIOUS.read_text().split(
            'CREATE OR REPLACE FUNCTION storage_v2_export_intelligence(', 1)[1].split(
            'CREATE OR REPLACE FUNCTION storage_v2_import_intelligence(', 1)[0]
        cls.sql('CREATE OR REPLACE FUNCTION fixture_export_before(' + definition)

    def export(self, source, generation, redaction, before=False):
        function = 'fixture_export_before' if before else 'storage_v2_export_intelligence'
        return self.sql(self.admin(
            f'SELECT {function}({source},{cards.literal(generation)},{cards.literal(redaction)})::TEXT;'))

    def assert_same_export(self, source, generation):
        for redaction in ('public', 'protected'):
            with self.subTest(source=source, generation=generation, redaction=redaction):
                self.assertEqual(self.export(source, generation, redaction),
                                 self.export(source, generation, redaction, before=True))

    def card_fixture(self, calls=1):
        args = cards.StructuralCardReuseTests.fixture(self)
        args['p_generic_card'] = (
            "jsonb_build_object('unicode','Grüße 東京 😀','escape',E'quote\"\\n\\t\\\\',"
            "'number',1.2300::NUMERIC,'large',1e40::NUMERIC,'small',1e-20::NUMERIC,"
            "'nested','{\"z\":[null,true,false,{},[]],\"a\":{\"é\":\"value\"}}'::JSONB,"
            "'long',repeat('public synthetic fixture ',80))")
        args['p_symbol_key'] = "'fixture-export-' || n"
        self.sql(self.admin('SELECT count(*) FROM generate_series(1,' + str(calls) + ') n '
                           'CROSS JOIN LATERAL storage_v2_put_structural_card_bundle(' +
                           ','.join(args.values()) + ') card;'))
        run, digest = self.sql(
            "SELECT item.run_id || ':' || encode(item.content_identity_sha256,'hex') "
            'FROM storage_v2_ingest_run_item item '
            f"WHERE artifact_version_id={args['p_artifact_version_id']}").split(':')
        self.complete_analysis(digest)
        self.commit(int(run), 1)
        return args

    def test_intelligence_provenance_retry_and_round_trip(self):
        # Exercise existing import, provenance, authorization and generation
        # checks against the new function before comparing the original export.
        super().test_intelligence_provenance_retry_and_round_trip()
        for source, generation in ((4, '1'), (4, '2'), (5, '1')):
            self.assert_same_export(source, generation)
        counts = json.loads(self.export(4, '1', 'public'))['payload']['record_counts']
        self.assertEqual(len(counts), 8)
        self.assertTrue(all(count > 0 for count in counts.values()), counts)

    def test_canonical_serialization_large_collection_and_empty_arrays(self):
        args = self.card_fixture(2500)
        source = args['p_source_id']
        self.assert_same_export(source, '1')
        public = json.loads(self.export(source, '1', 'public'))
        counts = public['payload']['record_counts']
        self.assertEqual(counts['cards'], 2500)
        self.assertTrue(all(value == 0 for key, value in counts.items() if key != 'cards'))
        protected = json.loads(self.export(source, '1', 'protected'))
        self.assertEqual(public['payload']['protected_payload_sha256'], protected['payload_sha256'])
        self.assertNotIn('Grüße', json.dumps(public, ensure_ascii=False))

    def test_migration_reapply_preserves_function_contract_and_semantic_rows(self):
        args = self.card_fixture()
        source = args['p_source_id']
        metadata = ("SELECT oid,proowner,proacl,provolatile,prosecdef,proconfig,prorettype "
                    "FROM pg_proc WHERE oid='storage_v2_export_intelligence(bigint,text,text)'::regprocedure")
        before = self.sql(metadata)
        identity = cards.StructuralCardReuseTests.data_identity(self)
        payload = self.export(source, '1', 'protected')
        for _ in range(2):
            self.file(MIGRATION)
            self.assertEqual(self.sql(metadata), before)
            self.assertEqual(cards.StructuralCardReuseTests.data_identity(self), identity)
            self.assertEqual(self.export(source, '1', 'protected'), payload)
        self.assert_same_export(source, '1')

    def test_repeated_order_keys_keep_every_structural_variant(self):
        args = self.card_fixture(2)
        args['p_symbol_key'] = "'fixture-export-1'"
        for ordinal in range(1, 9):
            args['p_structure'] = f"jsonb_build_object('kind','function','variant',{ordinal})"
            self.sql(self.admin(cards.StructuralCardReuseTests.call(args)))
        self.assert_same_export(args['p_source_id'], '1')
        payload = json.loads(self.export(args['p_source_id'], '1', 'protected'))['payload']
        self.assertEqual(len(payload['cards']), 10)
        self.assertEqual(sum(card['symbol_key'] == 'fixture-export-1' for card in payload['cards']), 9)

    def test_error_and_null_redaction_contract_is_unchanged(self):
        args = self.card_fixture()
        source = args['p_source_id']
        for actor, source_arg, generation, redaction in (
            (schema.OTHER_ID, source, "'1'", "'public'"),
            (schema.ADMIN_ID, source, "'999999'", "'public'"),
            (schema.ADMIN_ID, source, 'NULL', "'public'"),
            (schema.ADMIN_ID, 'NULL', "'1'", "'public'"),
            (schema.ADMIN_ID, source, "'1'", "'invalid'"),
            (schema.ADMIN_ID, source, "'1'", 'NULL'),
        ):
            observed = []
            for function in ('fixture_export_before', 'storage_v2_export_intelligence'):
                statement = self.actor(actor, f'SELECT {function}({source_arg},{generation},{redaction})')
                result = self.command('--set=VERBOSITY=sqlstate', '--command', statement, check=False)
                observed.append((result.returncode, result.stdout, result.stderr.splitlines()[0:1]))
            with self.subTest(actor=actor, source=source_arg, generation=generation, redaction=redaction):
                self.assertEqual(observed[0], observed[1])


if __name__ == '__main__':
    unittest.main()
