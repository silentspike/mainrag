"""Full-result, authority and complete-work checks for targeted intelligence."""
import json

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_intelligence_export_text as exports
from eval.storage_v2.schema import test_structural_card_reuse as cards

MIGRATION = schema.ROOT / 'migrations/053_storage_v2_targeted_intelligence_commands.sql'
SIGNATURE = 'storage_v2_intelligence_command(bigint,text,text,jsonb)'


class TargetedIntelligenceCommandTests(schema.ShadowIngestSchemaTests):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        definition = exports.PREVIOUS.read_text().split(
            'CREATE OR REPLACE FUNCTION storage_v2_intelligence_command(', 1)[1].split(
            'REVOKE INSERT, UPDATE, DELETE', 1)[0]
        cls.sql('CREATE OR REPLACE FUNCTION fixture_command_before(' + definition)

    def command_result(self, source, generation, command, query, before=False):
        function = 'fixture_command_before' if before else 'storage_v2_intelligence_command'
        return self.sql(self.admin(f'SELECT {function}({source},{cards.literal(generation)},'
                                  f'{cards.literal(command)},{cards.literal(json.dumps(query))}::JSONB)::TEXT;'))

    def assert_same_command(self, source, generation, command, query):
        with self.subTest(source=source, generation=generation, command=command, query=query):
            self.assertEqual(self.command_result(source, generation, command, query),
                             self.command_result(source, generation, command, query, before=True))

    def test_intelligence_provenance_retry_and_round_trip(self):
        super().test_intelligence_provenance_retry_and_round_trip()
        pairs = [
            ('card', {}), ('layers', {}), ('card', {'name': 'alpha'}),
            ('card', {'name': 'ALPHA'}), ('card', {'name': '%'}), ('card', {'name': '_'}),
            ('layers', {'layer': 'api'}), ('layers', {'layer': 'internal', 'name': 'alpha'}),
            ('card', {'resource': 'unknown', 'side_effect': 'unknown'}),
            ('card', {'layer': 'unknown', 'resource': 'missing'}),
            ('card', {'name': None, 'layer': '', 'resource': None, 'side_effect': ''}),
            ('card', {'name': 'missing'}), ('layers', None),
            ('explain', {'name': 'alpha'}), ('explain', {'name': 'crate::alpha'}),
            ('explain', {'name': 'beta'}), ('explain', {'name': 'missing'}), ('explain', {}),
            ('ownership', {'name': 'alpha'}), ('ownership', {'name': 'beta'}),
            ('ownership', {'name': 'missing'}), ('ownership', {'name': None}),
        ]
        for source, generation in ((4, '1'), (4, '2'), (5, '1')):
            for command, query in pairs:
                self.assert_same_command(source, generation, command, query)
        counts = json.loads(self.sql(self.admin(
            "SELECT storage_v2_export_intelligence(4,'1','public')->'payload'->'record_counts'")))
        self.assertEqual(len(counts), 8)
        self.assertTrue(all(value > 0 for value in counts.values()))

    def test_complete_large_card_collection_and_structural_variants(self):
        args = exports.IntelligenceExportTextTests.card_fixture(self, 2500)
        source = args['p_source_id']
        for command in ('card', 'layers'):
            self.assert_same_command(source, '1', command, {})
            self.assertEqual(len(json.loads(self.command_result(source, '1', command, {}))), 2500)
        args['p_symbol_key'] = "'fixture-export-1'"
        for ordinal in range(1, 9):
            args['p_structure'] = f"jsonb_build_object('kind','function','variant',{ordinal})"
            self.sql(self.admin(cards.StructuralCardReuseTests.call(args)))
        self.assert_same_command(source, '1', 'layers', {})
        result = json.loads(self.command_result(source, '1', 'layers', {}))
        self.assertEqual(len(result), 2508)
        self.assertEqual(sum(row['symbol_key'] == 'fixture-export-1' for row in result), 9)

    def test_commands_do_not_invoke_the_full_export(self):
        args = exports.IntelligenceExportTextTests.card_fixture(self, 3)
        source = args['p_source_id']
        queries = [('card', {}), ('layers', {}), ('explain', {}), ('ownership', {})]
        expected = [self.command_result(source, '1', command, query) for command, query in queries]
        signature = 'storage_v2_export_intelligence(bigint,text,text)'
        original = self.sql(f"SELECT pg_get_functiondef('{signature}'::REGPROCEDURE)")
        try:
            self.sql("""
CREATE OR REPLACE FUNCTION storage_v2_export_intelligence(
    p_source_id BIGINT, p_generation_selector TEXT, p_redaction TEXT DEFAULT 'public')
RETURNS JSONB LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,public SET row_security=off AS $guard$
BEGIN RAISE EXCEPTION 'full export must not execute for targeted commands'; END
$guard$;
""")
            for (command, query), value in zip(queries, expected):
                self.assertEqual(self.command_result(source, '1', command, query), value)
        finally:
            self.sql(original)

    def test_replay_preserves_metadata_semantic_rows_and_export_contract(self):
        args = exports.IntelligenceExportTextTests.card_fixture(self, 2)
        metadata = f"SELECT to_jsonb(p) - 'prosrc' FROM pg_proc p WHERE oid='{SIGNATURE}'::REGPROCEDURE"
        before = self.sql(metadata)
        identity = cards.StructuralCardReuseTests.data_identity(self)
        export = self.sql(self.admin(f"SELECT storage_v2_export_intelligence({args['p_source_id']},'1','protected')"))
        for _ in range(2):
            self.file(MIGRATION)
            self.assertEqual(self.sql(metadata), before)
            self.assertEqual(cards.StructuralCardReuseTests.data_identity(self), identity)
            self.assertEqual(self.sql(self.admin(
                f"SELECT storage_v2_export_intelligence({args['p_source_id']},'1','protected')")), export)

    def test_error_authorization_and_selector_precedence_are_unchanged(self):
        args = exports.IntelligenceExportTextTests.card_fixture(self)
        source = args['p_source_id']
        for actor, source_arg, generation, command in (
            (schema.OTHER_ID, source, "'1'", "'card'"),
            (schema.OTHER_ID, source, "'invalid'", "'unsupported'"),
            (schema.ADMIN_ID, source, "'999999'", "'layers'"),
            (schema.ADMIN_ID, source, "'invalid'", "'unsupported'"),
            (schema.ADMIN_ID, source, "'1'", "'unsupported'"),
            (schema.ADMIN_ID, source, "'1'", 'NULL'),
            (schema.ADMIN_ID, 'NULL', "'1'", "'card'"),
        ):
            errors = []
            for function in ('fixture_command_before', 'storage_v2_intelligence_command'):
                result = self.command('--set=VERBOSITY=sqlstate', '--command', self.actor(actor,
                    f"SELECT {function}({source_arg},{generation},{command},'{{}}')"), check=False)
                self.assertNotEqual(result.returncode, 0)
                errors.append(result.stderr.splitlines()[0])
            self.assertEqual(*errors)
