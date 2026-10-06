"""Focused index ownership, catalog delta and lost-ack tests; no production I/O."""
import copy
import importlib.util
import os
from pathlib import Path
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

SPEC = importlib.util.spec_from_file_location('exact_index_operator_tests',
    Path(__file__).with_name('exact-lexeme-indexes.py'))
O = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(O)


def original_catalog():
    return {'cores': [
        {'oid': 10, 'name': O.TABLES[0], 'tablespace': 0,
         'core': {'owner': 30, 'columns': [[2, 'fts_vectors', 0, 0, False, '', '', 100, None]]}},
        {'oid': 20, 'name': O.TABLES[1], 'tablespace': 0,
         'core': {'owner': 30, 'columns': [[3, 'terms', 0, 0, False, '', '', 100, None]]}}],
        'indexes': [], 'names': [], 'functions': [{'signature': 'fixture()', 'definition_sha256': 'a' * 64}],
        'function_contract': [{'signature': 'fixture()', 'definition': 'a' * 64}],
        'contract': [{'oid': 20, 'pin': 'b' * 64, 'actual': 'b' * 64},
                     {'oid': 90, 'pin': 'c' * 64, 'actual': 'c' * 64}],
        'state': {'generation_state_sha256': 'd' * 64, 'pointer_state_sha256': 'e' * 64},
        'default_collation_oid': 100}


def add_index(current, phase, valid=True):
    row = {'oid': 1000 + phase, 'table_oid': 10 if phase == 0 else 20,
           'name': O.INDEXES[phase], 'owner': 30, 'options': None, 'tablespace': 0,
           'method': 'gin', 'unique': False, 'primary_key': False, 'exclusion': False,
           'natts': 1, 'nkeys': 1, 'predicate': None, 'keys': '0',
           'expression': ('storage_v2_compact_lexical_exact_lexemes(fts_vectors)',
                          'storage_v2_compact_posting_exact_term_keys(terms)')[phase],
           'opclass': 'array_ops', 'opnamespace': 'pg_catalog', 'option': 0,
           'collation': 100 if phase == 0 else 0,
           'valid': valid, 'ready': valid, 'live': True, 'checkxmin': False,
           'definition': O.ddl(phase).replace(' CONCURRENTLY', '')}
    current['indexes'].append(row)
    current['names'].append({'schema': 'public', 'name': row['name'], 'oid': row['oid']})
    if phase == 1:
        current['contract'][0]['actual'] = 'f' * 64
    return row


def intent(phase):
    return {str(phase): {'ddl_sha256': O.digest(O.ddl(phase))}}


class ExactLexemeOwnershipTests(unittest.TestCase):
    def test_ci_transport_is_fixture_only_and_does_not_copy_private_environment(self):
        fixture = {'STORAGE_V2_TEST_SOCKET': '127.0.0.1', 'PGUSER': 'fixture',
                   'PGPASSWORD': 'fixture_only', 'PRIVATE_TOKEN': 'must-not-copy'}
        def observe(db):
            self.assertIn('127.0.0.1', db.command)
            self.assertEqual(db.command[-2:], ['-U', 'fixture'])
            self.assertEqual(O.clean_environment().get('PGPASSWORD'), 'fixture_only')
            self.assertNotIn('PRIVATE_TOKEN', O.clean_environment())
            return {'status': 'fixture-transport-observed'}
        with patch.dict(os.environ, fixture), patch.dict(os.environ, {'PGSERVICE': 'must-not-copy'}):
            with patch(__name__ + '._exercise_catalog_protocol', side_effect=observe):
                self.assertEqual(exercise_disposable_catalog_protocol(
                    'owned_fixture', Path('127.0.0.1'), '00000000-0000-4000-8000-000000000031'),
                    {'status': 'fixture-transport-observed'})
            self.assertNotIn('PGPASSWORD', O.clean_environment())
            for invalid in ('192.0.2.1', 'localhost', '127.0.0.1:5432'):
                with self.subTest(host=invalid), self.assertRaises(RuntimeError):
                    exercise_disposable_catalog_protocol(
                        'owned_fixture', Path(invalid), '00000000-0000-4000-8000-000000000031')
            with patch.dict(os.environ, {'PGUSER': 'unrelated-account'}), self.assertRaises(RuntimeError):
                exercise_disposable_catalog_protocol(
                    'owned_fixture', Path('127.0.0.1'), '00000000-0000-4000-8000-000000000031')

    def test_only_reviewed_constant_ddl_is_available(self):
        for phase in (0, 1):
            self.assertTrue(O.ddl(phase).startswith('CREATE INDEX CONCURRENTLY '))
            self.assertNotIn('IF NOT EXISTS', O.ddl(phase))
            self.assertTrue(O.ddl(phase, True).startswith('DROP INDEX CONCURRENTLY public.'))
        for phase in (True, -1, 2, '0', 'injected;DDL'):
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                O.ddl(phase)

    def test_valid_lost_ack_index_is_reconciled_only_with_durable_own_intent(self):
        old = original_catalog()
        now = copy.deepcopy(old)
        add_index(now, 0)
        self.assertTrue(O.delta(old, now, intent(0))[0][1])
        with self.assertRaisesRegex(RuntimeError, 'pre-DDL intent'):
            O.delta(old, now, {})
        # Actual column OIDs can be JSON strings. BYTEA[] expression keys have
        # no collation even while the original terms column remains collated.
        old['cores'][1]['core']['columns'][0][7] = '100'
        now = copy.deepcopy(old)
        row = add_index(now, 1)
        self.assertTrue(O.delta(old, now, intent(1))[1][1])
        row['collation'] = '0'
        self.assertTrue(O.delta(old, now, intent(1))[1][1])
        old['default_collation_oid'] = '100'
        now = copy.deepcopy(old)
        add_index(now, 0)
        self.assertTrue(O.delta(old, now, intent(0))[0][1])
        for invalid in (True, -1, '100;SQL', '01', 4294967296, 100.0, '١٠٠'):
            with self.subTest(oid=invalid), self.assertRaises(RuntimeError):
                O.catalog_oid(invalid)

    def test_posting_probe_requires_full_term_fixed_size_expression_not_raw_terms(self):
        old = original_catalog()
        now = copy.deepcopy(old)
        row = add_index(now, 1)
        self.assertIn('storage_v2_compact_posting_exact_term_keys(terms)', O.ddl(1))
        self.assertEqual(row['keys'], '0')
        self.assertEqual(row['collation'], 0)
        self.assertTrue(O.delta(old, now, intent(1))[1][1])
        # The former raw TEXT[] GIN is unsafe for accepted unbounded terms and
        # must be refused even if its name, table and ready flags match.
        row.update(keys='3', expression=None, collation=100)
        with self.assertRaisesRegex(RuntimeError, 'conflicting exact definition'):
            O.delta(old, now, intent(1))
        row.update(keys='0', expression='storage_v2_compact_posting_exact_term_keys(terms)', collation=100)
        with self.assertRaisesRegex(RuntimeError, 'collation differs'):
            O.delta(old, now, intent(1))

    def test_all_five_pins_and_three_helper_execution_contracts_are_mandatory(self):
        functions = []
        expected = {}
        for i, signature in enumerate(O.SIGNATURES):
            row = {'signature': signature, 'definition_sha256': 'a' * 64, 'metadata_sha256': 'b' * 64,
                   'owner': 'mainrag_v2_lexical_rank_owner', 'volatility': 's' if i == 2 else 'i',
                   'strict': i != 2, 'parallel': 's', 'definer': False, 'leakproof': False,
                   'support': 0, 'language': 'plpgsql' if i == 2 else 'sql',
                   'returns': ('text[]', 'bytea[]', 'boolean', 'record', 'record')[i],
                   'config': ['search_path=pg_catalog, public, pg_temp'],
                   'acl': [[role, 'EXECUTE', False] for role in
                       (['mainrag_v2_lexical_rank_owner', 'mainrag_v2_frontier_owner'] +
                        (['mainrag'] if i in (1, 2) else []))]}
            functions.append(row)
            expected[signature] = {k: row[k] for k in ('definition_sha256', 'metadata_sha256')}
        O.gate_helpers({'functions': functions}, expected)
        missing = dict(expected)
        del missing[O.SIGNATURES[1]]
        with self.assertRaisesRegex(RuntimeError, 'all five'):
            O.gate_helpers({'functions': functions}, missing)
        for field, value in (('returns', 'text[]'), ('strict', False), ('leakproof', True),
                             ('support', 1), ('language', 'plpgsql'), ('parallel', 'u')):
            changed = copy.deepcopy(functions)
            changed[1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'helper owner'):
                O.gate_helpers({'functions': changed}, expected)
        changed = copy.deepcopy(functions)
        changed[1]['acl'] = changed[1]['acl'][:2]
        with self.assertRaisesRegex(RuntimeError, 'helper owner'):
            O.gate_helpers({'functions': changed}, expected)

    def test_rollback_retains_all_three_helpers_and_restores_only_owned_indexes(self):
        old = original_catalog()
        session = SimpleNamespace(scalar=Mock(return_value=False))
        state = {'intents': {}}
        flow = O.Workflow(None, session, {'original': old}, state, lambda s: None, {})
        flow.reconcile = Mock(return_value={})
        flow.run_phase = Mock()
        with patch.object(O, 'catalog', return_value=old):
            flow.rollback()
        self.assertEqual(state['helpers_retained_for_installed_reader'], list(O.SIGNATURES[:3]))
        self.assertEqual(state['status'], 'ROLLED_BACK_ORIGINAL_CATALOG_HELPERS_RETAINED')
        flow.run_phase.assert_not_called()

    def test_invalid_index_is_retained_as_incomplete_and_never_rebuilt(self):
        old = original_catalog()
        now = copy.deepcopy(old)
        add_index(now, 0, valid=False)
        state = {'intents': intent(0)}
        session = SimpleNamespace(statement=Mock())
        saved = []
        flow = O.Workflow(None, session, {'original': old}, state, lambda s: saved.append(copy.deepcopy(s)), {})
        flow.run_phase = Mock()
        with patch.object(O, 'catalog', return_value=now), self.assertRaisesRegex(RuntimeError, 'invalid/incomplete'):
            flow.build()
        flow.run_phase.assert_not_called()
        self.assertEqual(saved[-1]['status'], 'INVALID_INDEX_RETAINED')

    def test_conflicting_index_definition_and_wrong_schema_are_drift(self):
        old = original_catalog()
        for key, value in (('table_oid', 999), ('owner', 99), ('options', ['fastupdate=off']),
                           ('predicate', 'true'), ('opclass', 'tsvector_ops'), ('collation', 999),
                           ('keys', '2'), ('tablespace', 999), ('unique', True)):
            now = copy.deepcopy(old)
            add_index(now, 0)[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                O.delta(old, now, intent(0))
        now = copy.deepcopy(old)
        add_index(now, 0)
        now['names'][0]['schema'] = 'other'
        with self.assertRaisesRegex(RuntimeError, 'same-name'):
            O.delta(old, now, intent(0))

    def test_original_core_functions_contract_and_pointers_must_remain_exact(self):
        old = original_catalog()
        for field in ('cores', 'functions', 'function_contract', 'state'):
            now = copy.deepcopy(old)
            now[field] = {'changed': True}
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'drifted'):
                O.delta(old, now, {})
        now = copy.deepcopy(old)
        now['contract'][1]['pin'] = '9' * 64
        with self.assertRaisesRegex(RuntimeError, 'unrelated SQL157'):
            O.delta(old, now, {})

    def test_original_indexes_cannot_disappear_change_or_gain_unrelated_additions(self):
        old = original_catalog()
        old['indexes'] = [{'oid': 500, 'name': 'original_fixture_index', 'valid': True}]
        for replacement in ([], [{'oid': 500, 'name': 'original_fixture_index', 'valid': False}],
                            old['indexes'] + [{'oid': 600, 'name': 'unrelated_index'}]):
            now = copy.deepcopy(old)
            now['indexes'] = replacement
            with self.subTest(replacement=replacement), self.assertRaises(RuntimeError):
                O.delta(old, now, {})

    def test_compact_pin_ack_is_narrow_compare_and_swap_not_blanket_resigning(self):
        old = original_catalog()
        now = copy.deepcopy(old)
        add_index(now, 1)
        session = SimpleNamespace(statement=Mock())
        state = {'intents': intent(1)}
        flow = O.Workflow(None, session, {'original': old}, state, lambda s: None, {})
        with patch.object(O, 'catalog', return_value=now):
            flow.reconcile()
        sql = session.statement.call_args.args[0]
        self.assertIn('WHERE relation_oid=20 AND identity_sha256=', sql)
        self.assertIn('IF NOT FOUND THEN RAISE', sql)
        self.assertIn('storage_v2_posting_conversion_require_operator()', sql)
        self.assertEqual(state['accepted_compact_pins'], ['f' * 64])

    def test_rollback_accepts_only_previously_recorded_pin_and_restores_original(self):
        old = original_catalog()
        after_drop = copy.deepcopy(old)
        after_drop['contract'][0]['pin'] = 'f' * 64
        with self.assertRaisesRegex(RuntimeError, 'neither original'):
            O.delta(old, after_drop, intent(1))
        self.assertEqual(O.delta(old, after_drop, intent(1), ['f' * 64]), {})
        state = {'intents': intent(1), 'accepted_compact_pins': ['f' * 64]}
        session = SimpleNamespace(statement=Mock())
        flow = O.Workflow(None, session, {'original': old}, state, lambda s: None, {})
        with patch.object(O, 'catalog', return_value=after_drop):
            flow.reconcile()
        sql = session.statement.call_args.args[0]
        self.assertIn("SET identity_sha256=decode('" + 'b' * 64, sql)
        self.assertNotIn('DROP FUNCTION', sql)

    def test_cancel_query_requires_all_owned_backend_fields_and_exact_statement(self):
        db = Mock()
        db.query.return_value = [{'cancelled': True}]
        owner = {'pid': 123, 'start': 'public-fixture-time', 'app': 'exact-fixture',
                 'db': 'fixture', 'role': 'postgres'}
        self.assertTrue(O.cancel_owned(db, owner, O.ddl(0) + ';'))
        sql, args = db.query.call_args.args
        self.assertEqual(args, (123, owner['start'], owner['app'], 'fixture', 'postgres', O.ddl(0) + ';'))
        for predicate in ('pid=%s', 'backend_start::text=%s', 'application_name=%s',
                          'datname=%s', 'usename=%s', "state='active'", 'query=%s'):
            self.assertIn(predicate, sql)
        db.query.return_value = []
        self.assertFalse(O.cancel_owned(db, owner, O.ddl(0) + ';'))

    def test_protocol_never_promotes_disconnection_or_wrong_ack_to_success(self):
        session = object.__new__(O.Session)
        session.pending = 'same-operation-ack'
        session.lines = queue.Queue()
        session.lines.put(None)
        with self.assertRaisesRegex(RuntimeError, 'UNKNOWN'):
            session.poll()
        session.lines.put('{"tag":"different-operation","value":"DONE"}')
        with self.assertRaisesRegex(RuntimeError, 'identity differs'):
            session.poll()
        session.lines.put('\n')
        self.assertEqual(session.poll(), (False, None))

    def test_drop_reserves_only_reviewed_drop_work_not_create_index_bytes(self):
        config = {'bounds': {'0': {'index_bytes': 50 * O.GIB, 'temp_bytes': O.GIB,
                                  'wal_bytes': O.GIB, 'ram_bytes': 4 * O.GIB, 'seconds': 60}},
                  'drop_bounds': {'0': {'temp_bytes': 1024, 'wal_bytes': 1024,
                                       'ram_bytes': 1024, 'seconds': 60}},
                  'estimates': {'0': {'input_relation_bytes': O.GIB,
                      'conservative_method': 'public fixture complete array bound',
                      'complete_existing_cache_arrays': True}}}
        seen = []
        def readback(config, db, growth):
            seen.append(growth)
            return {'used': 100, 'free': 100 * O.GIB, 'wal': 0,
                    'ram': 8 * O.GIB, 'temp_free': 100 * O.GIB}
        with patch.object(O, 'resource_observation', side_effect=readback):
            O.admission(config, None, 0, drop=True)
            O.admission(config, None, 0)
        self.assertEqual(seen, [2048, 52 * O.GIB])
        del config['estimates']['0']['complete_existing_cache_arrays']
        with self.assertRaisesRegex(RuntimeError, 'unknown index size'):
            O.bounds(config, 0)

    def test_wal_low_boundary_and_conservative_burst_both_apply(self):
        config = {'bounds': {'0': {'index_bytes': O.GIB, 'temp_bytes': O.GIB,
                    'wal_bytes': 4 * O.GIB, 'ram_bytes': O.GIB, 'seconds': 60}},
                  'estimates': {'0': {'input_relation_bytes': O.GIB,
                    'conservative_method': 'public fixture complete array bound',
                    'complete_existing_cache_arrays': True}}}
        for wal in (24 * O.GIB + 1, 27 * O.GIB):
            with patch.object(O, 'resource_observation', return_value={'wal': wal}), self.assertRaisesRegex(RuntimeError, 'drained'):
                O.admission(config, None, 0)


def exercise_disposable_catalog_protocol(database, socket, user_id):
    """Called by the existing owned SQL159 schema fixture, not by production CLI.

    Reuses that disposable database; no extra cluster or model/performance work.
    Resource/runtime policy tests stay pure because this is a public tiny fixture.
    Exercises the real query parser, held session lock, autocommit CIC, catalog
    comparison, narrow pin update, reverse DROP and original-contract restore.
    """
    if Path(socket).is_absolute():
        db = O.LocalDatabase({'database': database, 'socket': str(socket),
                             'user_id': user_id, 'peer_admin_via_sudo': False})
        return _exercise_catalog_protocol(db)
    # The existing public CI service uses this exact loopback fixture account.
    # This adapter is test-only: the production constructor/environment still
    # rejects TCP and credentials. Reuse the same disposable database and real
    # JSON/session/CIC implementation, without an additional PostgreSQL cluster.
    if str(socket) != '127.0.0.1' or os.environ.get('STORAGE_V2_TEST_SOCKET') != str(socket) \
            or os.environ.get('PGUSER') != 'fixture' or os.environ.get('PGPASSWORD') != 'fixture_only':
        raise RuntimeError('only the declared public CI fixture transport is accepted')
    db = object.__new__(O.LocalDatabase)
    db.database, db.user_id = database, str(uuid.UUID(user_id))
    db.command = ['psql', '-X', '--no-psqlrc', '-qAt', '-v', 'ON_ERROR_STOP=1',
                  '-h', '127.0.0.1', '-d', database, '-U', 'fixture']
    environment = O.clean_environment() | {'PGUSER': 'fixture', 'PGPASSWORD': 'fixture_only'}
    with patch.object(O, 'clean_environment', return_value=environment):
        return _exercise_catalog_protocol(db)


def _exercise_catalog_protocol(db):
    operation = str(uuid.uuid4())
    session = O.Session(db, operation)
    try:
        session.statement('SELECT public.storage_v2_posting_conversion_require_operator()')
        old = O.catalog(db)
        old['default_collation_oid'] = db.query("SELECT 'default'::regcollation::oid::bigint oid")[0]['oid']
        if old['names']:
            raise AssertionError('disposable smoke must start before its first exact indexes')
        state = {'operation_id': operation, 'intents': {}}
        flow = O.Workflow(db, session, {'original': old}, state, lambda s: None, {})
        for phase in (0, 1):
            state['intents'].update(intent(phase))
            session.statement(O.ddl(phase))
            additions = flow.reconcile()
            if not additions[phase][1]:
                raise AssertionError('actual concurrent index not valid and ready')
        if session.scalar('SELECT public.storage_v2_exact_lexeme_probes_ready()') is not True:
            raise AssertionError('actual two-index ready gate not true')
        for phase in (1, 0):
            session.statement(O.ddl(phase, drop=True))
            flow.reconcile()
        if O.catalog(db)['contract'] != old['contract']:
            raise AssertionError('actual reverse DROP failed to restore original contract')
        if session.scalar('SELECT public.storage_v2_exact_lexeme_probes_ready()') is not False:
            raise AssertionError('installed fallback gate not false after actual DROP')
        return {'status': 'PASS', 'sequential_concurrent_indexes': 2,
                'original_catalog_restored': True, 'helpers_retained': True}
    finally:
        session.close()


if __name__ == '__main__':
    unittest.main()
