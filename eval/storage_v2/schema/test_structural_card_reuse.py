"""PostgreSQL checks for authorized, immutable structural-card reuse."""
from __future__ import annotations

import json
import re
import subprocess
import uuid

from eval.storage_v2.schema import test_shadow_ingest_schema as schema
from eval.storage_v2.schema import test_search_document_reuse as reuse


MIGRATION = schema.ROOT / 'migrations/047_storage_v2_structural_card_reuse.sql'
PREVIOUS = schema.ROOT / 'migrations/041_storage_v2_structural_card_bundle.sql'
TABLES = ('storage_v2_symbol', 'storage_v2_symbol_occurrence',
          'storage_v2_intelligence_analysis', 'storage_v2_symbol_card')


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def json_literal(value: object) -> str:
    return literal(json.dumps(value)) + '::JSONB'


class StructuralCardReuseTests(schema.ShadowIngestSchemaTests):
    start_client = reuse.SearchDocumentReuseTests.start_client
    wait_for_client = reuse.SearchDocumentReuseTests.wait_for_client

    def setUp(self) -> None:
        self.file(MIGRATION)

    def fixture(self) -> dict[str, str]:
        source_id = int(self.sql('SELECT max(id)+1 FROM sources'))
        self.sql(f"INSERT INTO sources(id,name,type,path) VALUES ({source_id},"
                 f"'synthetic-card-{source_id}','fixture','synthetic-card-{source_id}'); "
                 f"INSERT INTO fixture_source_access VALUES ('{schema.WRITER_ID}',{source_id},TRUE,TRUE)")
        content = f'fn synthetic_card_{source_id}() {{}}'
        node, view, digest = self.make_projection(content)
        run = self.begin(source_id, uuid.uuid4().hex * 2, uuid.uuid4().hex * 2)
        self.stage(run, 'synthetic-card.rs', content, node, view, digest)
        artifact, occurrence = self.sql('SELECT artifact_version_id || \':\' || occurrence_id '
                                        f'FROM storage_v2_ingest_run_item WHERE run_id={run}').split(':')
        return {
            'p_source_id': str(source_id), 'p_artifact_version_id': artifact, 'p_occurrence_id': occurrence,
            'p_symbol_key': literal('synthetic-card'), 'p_language': literal('rust'),
            'p_symbol_kind': literal('function'), 'p_qualified_name': literal('crate::synthetic_card'),
            'p_signature': literal('fn synthetic_card()'), 'p_documentation': 'NULL',
            'p_visibility': literal('private'), 'p_structure': json_literal({'kind': 'function'}),
            'p_source_span': json_literal({'line_start': 1, 'line_end': 1}),
            'p_analysis_profile_id': literal('synthetic-card-v1'),
            'p_output_sha256': "decode('" + 'aa' * 32 + "','hex')",
            'p_generic_card': json_literal({'name': 'synthetic_card'}),
            'p_domain_fields': json_literal({}), 'p_field_provenance': json_literal({}),
        }

    @staticmethod
    def call(arguments: dict[str, str]) -> str:
        return 'SELECT id FROM storage_v2_put_structural_card_bundle(' + ','.join(arguments.values()) + ');'

    def data_identity(self) -> str:
        return self.sql(';'.join(
            "SELECT md5(COALESCE(jsonb_agg(to_jsonb(row_value) ORDER BY to_jsonb(row_value)::TEXT)::TEXT,'')) "
            f'FROM {table} row_value' for table in TABLES))

    def bulk_query(self, args: dict[str, str], table: str) -> str:
        arguments = {**args, 'p_symbol_key': 'input.symbol_key'}
        return (f'SELECT count(bundle.id) FROM {table} input CROSS JOIN LATERAL '
                'storage_v2_put_structural_card_bundle(' + ','.join(arguments.values()) + ') bundle;')

    def bulk_fixture(self, table: str, calls: int) -> dict[str, str]:
        args = self.fixture()
        self.sql(f'CREATE TABLE {table}(ordinal INTEGER PRIMARY KEY, symbol_key TEXT NOT NULL); '
                 f"INSERT INTO {table} SELECT n,'synthetic-bulk-' || n FROM generate_series(1,{calls}) n; "
                 f'GRANT SELECT ON {table} TO storage_v2_shadow_worker;')
        return args

    def test_complete_lookup_uses_bounded_generic_index_probes(self) -> None:
        args = self.bulk_fixture('fixture_card_plan_inputs', 1000)
        self.assertEqual(self.sql(self.admin(self.bulk_query(args, 'fixture_card_plan_inputs'))), '1000')
        versions = list(args.values())[:12]
        versions[3] = literal('synthetic-bulk-1')
        versions[7] = "'synthetic structural alternative ' || n"
        self.assertEqual(self.sql(self.admin(
            'SELECT count(occ.id) FROM generate_series(1,1000) n CROSS JOIN LATERAL '
            'storage_v2_put_symbol_occurrence(' + ','.join(versions) + ') occ;')), '1000')
        self.sql(';'.join(f'ANALYZE {table}' for table in TABLES))
        first = self.sql(
            "SELECT json_build_object('identity',encode(symbol.identity_sha256,'hex'),"
            "'structural',encode(occ.structural_sha256,'hex'),'normalized',card.normalized_output) "
            'FROM storage_v2_symbol symbol JOIN storage_v2_symbol_occurrence occ ON occ.symbol_id=symbol.id '
            'JOIN storage_v2_symbol_card card ON card.symbol_occurrence_id=occ.id '
            f"WHERE symbol.source_id={args['p_source_id']} AND symbol.symbol_key='synthetic-bulk-1'")
        hashes = json.loads(first)
        values = {**args, 'p_symbol_key': literal('synthetic-bulk-1'),
                  'v_identity': f"decode('{hashes['identity']}','hex')",
                  'v_structural': f"decode('{hashes['structural']}','hex')",
                  'v_normalized': json_literal(hashes['normalized']),
                  'v_unknown_domain': json_literal({'layer': 'unknown', 'side_effect': 'unknown',
                                                   'resource': 'unknown', 'delegation_target': 'unknown'})}
        definition = self.sql("SELECT pg_get_functiondef(oid) FROM pg_proc "
                              "WHERE proname='storage_v2_put_structural_card_bundle'")
        lookup = re.search(r'SELECT symbol_occurrence\.\* INTO v_symbol_occurrence.*?;',
                           definition, re.DOTALL)
        self.assertIsNotNone(lookup)
        self.assertEqual(lookup.group().count('OFFSET 0'), 4,
                         'cold plans must not start from a broad analysis/card profile')
        query = lookup.group().replace(' INTO v_symbol_occurrence', '')
        positions = {key: f'${index}' for index, key in enumerate(values, 1)}
        query = re.sub(r'\b[pv]_[a-z_0-9]+\b', lambda match: positions[match.group()], query)
        types = ['BIGINT'] * 3 + ['TEXT'] * 7 + ['JSONB'] * 2 + ['TEXT', 'BYTEA'] + ['JSONB'] * 3
        types.extend(['BYTEA', 'BYTEA', 'JSONB', 'JSONB'])
        plan = json.loads(self.sql(
            'SET plan_cache_mode=force_generic_plan; '
            f"PREPARE fixture_card_lookup({','.join(types)}) AS {query} "
            'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '
            f"EXECUTE fixture_card_lookup({','.join(values.values())});"))[0]['Plan']

        def nodes(node):
            yield node
            for child in node.get('Plans', []):
                yield from nodes(child)

        self.assertEqual(plan['Actual Rows'], 1)
        scans = {node['Relation Name']: node for node in nodes(plan) if 'Relation Name' in node}
        for table in TABLES:
            with self.subTest(table=table):
                self.assertIn(table, scans)
                self.assertIn(scans[table]['Node Type'], ('Index Scan', 'Index Only Scan'))
                self.assertEqual(scans[table]['Actual Rows'], 1)
                self.assertEqual(scans[table].get('Rows Removed by Filter', 0), 0)
        symbol_key = scans['storage_v2_symbol']['Index Cond']
        occurrence_key = scans['storage_v2_symbol_occurrence']['Index Cond']
        self.assertIn('source_id = $1', symbol_key)
        # Either complete unique key is bounded. Full symbol text equality is
        # still checked when the planner selects the source/digest index.
        self.assertTrue('symbol_key = $4' in symbol_key or 'identity_sha256 = $18' in symbol_key,
                        symbol_key)
        self.assertIn('artifact_version_id = $2', occurrence_key)
        self.assertIn('structural_sha256 = $19', occurrence_key)

    def test_complete_reuse_performs_no_insert_and_preserves_sequences(self) -> None:
        args = self.fixture()
        call = self.admin(self.call(args))
        identity = self.sql(call)
        before = self.data_identity()
        sequence_sql = ('SELECT last_value,is_called FROM storage_v2_symbol_id_seq; '
                        'SELECT last_value,is_called FROM storage_v2_symbol_occurrence_id_seq')
        sequences = self.sql(sequence_sql)
        self.sql("CREATE FUNCTION fixture_forbid_card_write() RETURNS trigger LANGUAGE plpgsql AS $$ "
                 "BEGIN RAISE EXCEPTION 'unexpected complete-bundle write'; END $$;")
        try:
            for table in TABLES:
                self.sql(f'CREATE TRIGGER fixture_forbid_card_write BEFORE INSERT OR UPDATE OR DELETE '
                         f'ON {table} FOR EACH STATEMENT EXECUTE FUNCTION fixture_forbid_card_write()')
            for domain in ({}, {'layer': 'unknown'}, {
                'layer': 'unknown', 'side_effect': 'unknown', 'resource': 'unknown',
                'delegation_target': 'unknown',
            }):
                self.assertEqual(self.sql(self.admin(self.call({**args, 'p_domain_fields': json_literal(domain)}))), identity)
            self.assertEqual(self.sql(self.actor(schema.WRITER_ID, self.call(args))), identity)
            self.assertEqual(self.data_identity(), before)
            self.assertEqual(self.sql(sequence_sql), sequences)
        finally:
            for table in TABLES:
                self.sql(f'DROP TRIGGER IF EXISTS fixture_forbid_card_write ON {table}')
            self.sql('DROP FUNCTION fixture_forbid_card_write()')

    def test_negative_inputs_retain_the_previous_error_contract(self) -> None:
        args = self.fixture()
        self.sql(self.admin(self.call(args)))
        before = self.data_identity()
        changes = [
            ('p_source_id', 'NULL'), ('p_source_id', '2'),
            ('p_artifact_version_id', 'NULL'), ('p_occurrence_id', 'NULL'),
            ('p_artifact_version_id', '-1'), ('p_occurrence_id', '-1'),
            ('p_symbol_key', "''"), ('p_language', "''"), ('p_language', "'python'"),
            ('p_symbol_kind', "'class'"), ('p_qualified_name', "'different'"),
            ('p_structure', 'NULL'), ('p_source_span', 'NULL'),
            ('p_analysis_profile_id', 'NULL'), ('p_analysis_profile_id', "''"),
            ('p_output_sha256', 'NULL'), ('p_output_sha256', "decode('aa','hex')"),
            ('p_output_sha256', "decode('" + 'bb' * 32 + "','hex')"),
            ('p_generic_card', 'NULL'), ('p_generic_card', "'[]'::JSONB"),
            ('p_generic_card', json_literal({'name': 'collision'})),
            ('p_domain_fields', 'NULL'), ('p_domain_fields', "'[]'::JSONB"),
            ('p_domain_fields', json_literal({'unexpected': 'unknown'})),
            ('p_domain_fields', json_literal({'layer': 'known-without-profile'})),
            ('p_domain_fields', json_literal({'layer': 1})),
            ('p_field_provenance', 'NULL'), ('p_field_provenance', "'[]'::JSONB"),
            ('p_field_provenance', json_literal({'layer': {'evidence': 'unsupported'}})),
        ]
        variants = [self.admin(self.call({**args, key: value})) for key, value in changes]
        variants.append(self.actor(schema.OTHER_ID, self.call(args)))
        observed = []
        for migration in (PREVIOUS, MIGRATION):
            self.file(migration)
            errors = []
            for index, statement in enumerate(variants):
                with self.subTest(migration=migration.name, case=index):
                    result = self.command('--set=VERBOSITY=verbose', '--command', statement, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    message = re.search(r'ERROR:\s+([A-Z0-9]{5}): (.*)', result.stderr)
                    self.assertIsNotNone(message, result.stderr)
                    errors.append(message.groups())
            observed.append(errors)
            self.assertEqual(self.data_identity(), before)
        self.assertEqual(*observed)

    def test_missing_card_and_retryable_analysis_use_the_atomic_writer(self) -> None:
        for status in ('complete', 'pending', 'failed'):
            with self.subTest(status=status):
                args = self.fixture()
                identity = self.sql(self.admin(self.call(args)))
                self.sql(f'DELETE FROM storage_v2_symbol_card WHERE symbol_occurrence_id={identity}')
                if status != 'complete':
                    error = "'synthetic-retry'" if status == 'failed' else 'NULL'
                    self.sql(f"UPDATE storage_v2_intelligence_analysis SET status='{status}',"
                             f'output_sha256=NULL,error_code={error} WHERE symbol_occurrence_id={identity}')
                self.assertEqual(self.sql(self.admin(self.call(args))), identity)
                self.assertEqual(self.sql('SELECT status || \':\' || attempt_count FROM storage_v2_intelligence_analysis '
                                          f'WHERE symbol_occurrence_id={identity}'),
                                 'complete:1' if status == 'complete' else 'complete:2')
                self.assertEqual(self.sql('SELECT count(*) FROM storage_v2_symbol_card '
                                          f'WHERE symbol_occurrence_id={identity}'), '1')

    def test_null_and_empty_structural_fields_keep_legacy_identity(self) -> None:
        args = {**self.fixture(), 'p_signature': 'NULL', 'p_visibility': 'NULL'}
        identity = self.sql(self.admin(self.call(args)))
        before = self.data_identity()
        for field in ('p_signature', 'p_documentation', 'p_visibility'):
            self.assertEqual(self.sql(self.admin(self.call({**args, field: "''"}))), identity)
        self.assertEqual(self.data_identity(), before)

    def test_reapplication_retains_owner_acl_and_cold_write(self) -> None:
        identity_sql = ("SELECT proowner::regrole::TEXT || ':' || proacl::TEXT FROM pg_proc "
                        "WHERE proname='storage_v2_put_structural_card_bundle'")
        before = self.sql(identity_sql)
        self.file(MIGRATION)
        self.file(MIGRATION)
        self.assertEqual(self.sql(identity_sql), before)
        self.assertEqual(self.sql("SELECT count(*) FROM pg_proc, LATERAL aclexplode(proacl) acl "
                                 "WHERE proname='storage_v2_put_structural_card_bundle' AND acl.grantee=0"), '0')
        args = self.fixture()
        self.assertGreater(int(self.sql(self.admin(self.call(args)))), 0)

    def test_concurrent_winner_uses_conflict_readback_and_rejects_collision(self) -> None:
        self.sql('CREATE TABLE fixture_card_barrier(released BOOLEAN NOT NULL); '
                 'INSERT INTO fixture_card_barrier VALUES(FALSE); '
                 'GRANT SELECT ON fixture_card_barrier TO storage_v2_shadow_worker')
        for collision in (False, True):
            with self.subTest(collision=collision):
                args = self.fixture()
                self.sql('UPDATE fixture_card_barrier SET released=FALSE')
                winner_name, loser_name = 'card-winner-' + uuid.uuid4().hex, 'card-loser-' + uuid.uuid4().hex
                winner = self.start_client(winner_name, self.admin(
                    "BEGIN; SET LOCAL statement_timeout='30s'; " + self.call(args) +
                    'DO $$ BEGIN WHILE NOT (SELECT released FROM fixture_card_barrier) LOOP '
                    'PERFORM pg_sleep(0.05); END LOOP; END $$; COMMIT;'))
                loser = None
                try:
                    self.wait_for_client(winner_name, 'PgSleep', winner)
                    other = {**args, 'p_generic_card': json_literal({'name': 'collision'})} if collision else args
                    loser = self.start_client(loser_name, self.admin(
                        "SET statement_timeout='30s'; " + self.call(other)))
                    self.wait_for_client(loser_name, 'transactionid', loser)
                    self.sql('UPDATE fixture_card_barrier SET released=TRUE')
                    winner_out, winner_error = winner.communicate(timeout=10)
                    loser_out, loser_error = loser.communicate(timeout=10)
                    self.assertEqual(winner.returncode, 0, winner_error)
                    if collision:
                        self.assertNotEqual(loser.returncode, 0)
                        self.assertIn('analysis profile output collision', loser_error)
                    else:
                        self.assertEqual(loser.returncode, 0, loser_error)
                        self.assertEqual(loser_out.strip(), winner_out.strip())
                finally:
                    self.sql('UPDATE fixture_card_barrier SET released=TRUE')
                    for process in (loser, winner):
                        if process is not None:
                            try:
                                process.communicate(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.communicate(timeout=5)


# The inherited storage/ingest/retrieval tests are intentionally part of this
# module's aggregate gate, not silently filtered out by the optimization suite.
