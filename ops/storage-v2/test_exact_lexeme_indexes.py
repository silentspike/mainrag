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


def guarded_config():
    phase = {'index_bytes': O.GUARDED_INDEX_CAP, 'temp_bytes': O.GIB,
             'wal_bytes': 4 * O.GIB, 'ram_bytes': O.GIB, 'seconds': 60}
    estimate = {'input_relation_bytes': O.GIB, 'estimate_mode': 'guarded_complete_build',
                'complete_existing_cache_arrays': False,
                'guarded_complete_build': {'unknown_final_index_size': True,
                    'reactive_monitor_may_overshoot': True, 'retain_incomplete_index': True,
                    'one_attempt_no_automatic_retry': True,
                    'overshoot_reserve_bytes': phase['index_bytes'] + phase['temp_bytes'] + phase['wal_bytes']}}
    return {'bounds': {str(i): copy.deepcopy(phase) for i in (0, 1)},
            'estimates': {str(i): copy.deepcopy(estimate) for i in (0, 1)}}


def guarded_profile():
    return {'version': 180004, 'block_bytes': 8192, 'segment_bytes': 16 * 1024 ** 2}


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
            return {'used': 100, 'free': 100 * O.GIB, 'wal': 0, 'wal_ready': 0,
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
            with patch.object(O, 'resource_observation', return_value={'wal_ready': wal}), self.assertRaisesRegex(RuntimeError, 'drained'):
                O.admission(config, None, 0)

    def test_reusable_wal_stock_is_not_backlog_and_both_budgets_remain_enforced(self):
        config = {'storage_root': '/', 'temp_root': '/',
                  'bounds': {'0': {'index_bytes': O.GIB, 'temp_bytes': O.GIB,
                    'wal_bytes': 4 * O.GIB, 'ram_bytes': O.GIB, 'seconds': 60}},
                  'estimates': {'0': {'input_relation_bytes': O.GIB,
                    'conservative_method': 'public fixture complete array bound',
                    'complete_existing_cache_arrays': True}}}
        db = Mock()
        db.capacity.block_sectors = 128
        db.capacity.observe.return_value = {'pool_size_bytes': 1024 * O.GIB,
            'data_percent_before_build': 10, 'metadata_percent_before_build': 1,
            'autoextend_threshold_percent': 80}
        observed = {'directory': '/', 'temp_spaces': '', 'default_space': '',
                    'default_database_space': True, 'wal': 30 * O.GIB, 'wal_ready': 0}
        db.query.return_value = [observed]
        with patch.object(O.shutil, 'disk_usage', return_value=SimpleNamespace(free=100 * O.GIB)), \
                patch.object(Path, 'read_text', return_value='MemAvailable: 8388608 kB\n'):
            start = O.admission(config, db, 0)
            self.assertEqual(start['wal'], 30 * O.GIB)
            self.assertEqual(start['wal_ready'], 0)
            self.assertEqual(start['wal_absolute_ceiling'], 34 * O.GIB)
            self.assertEqual(start['wal_ready_absolute_ceiling'], 4 * O.GIB)
            query = db.query.call_args.args[0]
            self.assertIn('pg_ls_waldir()', query)
            self.assertIn('public.storage_v2_local_wal_ready_bytes()', query)
            for stock, ready, message in ((30 * O.GIB, 28 * O.GIB, 'queued WAL'),
                                          (32 * O.GIB + 1, 0, 'hard stock32'),
                                          (30 * O.GIB, -1, 'observation is invalid'),
                                          (True, 0, 'observation is invalid')):
                observed.update(wal=stock, wal_ready=ready)
                with self.subTest(stock=stock, ready=ready), self.assertRaisesRegex(RuntimeError, message):
                    O.admission(config, db, 0)
        # Archived segments can be reused without new allocation. A growing
        # archive queue still must remain within the reviewed phase burst.
        resource = dict(start)
        db.query.return_value = [{'bytes': 0}]
        with patch.object(O, 'resource_observation', return_value=resource):
            O.monitor_budget(config, db, 0, start, O.time.monotonic())
            resource['wal_ready'] = start['wal_ready_absolute_ceiling'] + 1
            with self.assertRaisesRegex(RuntimeError, 'reviewed physical/WAL'):
                O.monitor_budget(config, db, 0, start, O.time.monotonic())
            resource.update(wal_ready=0, wal=32 * O.GIB + 1)
            with self.assertRaisesRegex(RuntimeError, 'reviewed physical/WAL'):
                O.monitor_budget(config, db, 0, start, O.time.monotonic())

    def test_guarded_construction_is_explicit_and_covers_terminal_page_wal(self):
        config = guarded_config()
        self.assertEqual(O.bounds(config, 0), config['bounds']['0'])
        self.assertEqual(O.terminal_page_wal_bound(O.GUARDED_INDEX_CAP), 4 * O.GIB)
        self.assertGreater(O.terminal_page_wal_bound(O.GUARDED_INDEX_CAP + 1), 4 * O.GIB)
        variants = []
        for field in ('unknown_final_index_size', 'reactive_monitor_may_overshoot',
                      'retain_incomplete_index', 'one_attempt_no_automatic_retry'):
            changed = copy.deepcopy(config)
            changed['estimates']['0']['guarded_complete_build'][field] = False
            variants.append(changed)
        for key, value in (('estimate_mode', 'silent-size-assumption'),
                           ('complete_existing_cache_arrays', True)):
            changed = copy.deepcopy(config)
            changed['estimates']['0'][key] = value
            variants.append(changed)
        for field, value in (('index_bytes', O.GUARDED_INDEX_CAP + 1),
                             ('wal_bytes', 4 * O.GIB - 1)):
            changed = copy.deepcopy(config)
            changed['bounds']['0'][field] = value
            variants.append(changed)
        changed = copy.deepcopy(config)
        changed['estimates']['0']['guarded_complete_build']['overshoot_reserve_bytes'] -= 1
        variants.append(changed)
        for changed in variants:
            with self.subTest(estimate=changed['estimates']['0']), self.assertRaises(RuntimeError):
                O.bounds(changed, 0)
        # The default prediction contract remains closed on unknown arrays.
        changed = copy.deepcopy(config)
        del changed['estimates']['0']['estimate_mode']
        with self.assertRaisesRegex(RuntimeError, 'complete-cache conservative'):
            O.bounds(changed, 0)

    def test_guarded_admission_reserves_overshoot_and_pins_page_architecture(self):
        config = guarded_config()
        db = Mock()
        db.query.return_value = [guarded_profile()]
        seen = []
        def observation(config, db, growth):
            seen.append(growth)
            return {'used': 100, 'free': 100 * O.GIB, 'wal': 2 * O.GIB,
                    'wal_ready': 0, 'ram': 8 * O.GIB, 'temp_free': 100 * O.GIB}
        with patch.object(O, 'resource_observation', side_effect=observation):
            start = O.admission(config, db, 0)
            allocation = O.GUARDED_INDEX_CAP + 5 * O.GIB
            self.assertEqual(seen, [2 * allocation])
            self.assertEqual(start['pool_absolute_ceiling'], 100 + allocation)
            self.assertEqual(start['overshoot_reserve_bytes'], allocation)
            self.assertEqual(start['wal_absolute_ceiling'], 6 * O.GIB)
            for field, value in (('version', 170009), ('block_bytes', 16384),
                                 ('segment_bytes', 64 * 1024 ** 2)):
                db.query.return_value = [dict(guarded_profile(), **{field: value})]
                with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'PostgreSQL18'):
                    O.admission(config, db, 0)

    def test_guarded_cap_cancels_owned_attempt_and_never_implicitly_restarts_it(self):
        config = guarded_config()
        db = Mock()
        session = Mock()
        session.identity = {'pid': 123, 'start': 'fixture-time', 'app': 'exact-owned-fixture',
                            'db': 'fixture', 'role': 'postgres'}
        session.poll.return_value = (False, None)
        state = {'intents': {}}
        flow = O.Workflow(db, session, {'package_sha256': 'a' * 64}, state, lambda s: None, config)
        flow.reconcile = Mock()
        with patch.object(O, 'runtime_gate'), patch.object(O, 'writer_gate'), \
                patch.object(O, 'admission', return_value={'guarded_runtime_profile': guarded_profile()}), \
                patch.object(O, 'monitor_budget', side_effect=RuntimeError('terminal-page WAL envelope')), \
                patch.object(O, 'cancel_owned', return_value=True) as cancelled:
            with self.assertRaisesRegex(RuntimeError, 'terminal-page WAL'):
                flow.run_phase(0)
            cancelled.assert_called_once_with(db, session.identity, O.ddl(0) + ';')
            self.assertEqual(state['status'], 'OUTCOME_UNKNOWN_RECONCILE_REQUIRED')
            self.assertTrue(state['cancelled_exact_owned_backend'])
            self.assertEqual(state['intents']['0']['ddl_sha256'], O.digest(O.ddl(0)))
            with self.assertRaisesRegex(RuntimeError, 'earlier phase intent'):
                flow.run_phase(0)
        session.begin.assert_called_once_with(O.ddl(0))
        flow.reconcile.assert_not_called()
        statements = ' '.join(c.args[0] for c in session.statement.call_args_list)
        self.assertIn('temp_file_limit=', statements)
        self.assertIn('max_parallel_workers_per_gather=0', statements)

    def test_valid_lost_ack_guarded_index_requires_actual_complete_size_proof(self):
        config = guarded_config()
        old = original_catalog()
        now = copy.deepcopy(old)
        add_index(now, 0)
        state = {'intents': {'0': dict(intent(0)['0'], before={
                    'guarded_runtime_profile': guarded_profile()})}}
        db = Mock()
        db.query.return_value = [{'bytes': O.GUARDED_INDEX_CAP + 8192}]
        session = SimpleNamespace(statement=Mock(), scalar=Mock(return_value=True))
        flow = O.Workflow(db, session, {'original': old}, state, lambda s: None, config)
        with patch.object(O, 'catalog', return_value=now), self.assertRaisesRegex(RuntimeError, 'retain and reconcile'):
            flow.build()
        self.assertEqual(state['status'], 'COMPLETE_BUILD_PROOF_REJECTED_INDEX_RETAINED')
        self.assertFalse(state['complete_build_proofs']['0']['admitted'])
        session.statement.assert_not_called()
        # A same-operation valid acknowledgement below the cap proves complete
        # construction; it does not turn the original unknown size into a guess.
        add_index(now, 1)
        now['contract'][0]['pin'] = now['contract'][0]['actual']
        state['accepted_compact_pins'] = [now['contract'][0]['actual']]
        state['intents']['1'] = dict(intent(1)['1'], before={'guarded_runtime_profile': guarded_profile()})
        db.query.return_value = [{'bytes': O.GIB}]
        with patch.object(O, 'catalog', return_value=now):
            flow.build()
        self.assertEqual(state['status'], 'COMPLETE_TWO_EXACT_INDEXES_READY')
        self.assertEqual(set(state['complete_build_proofs']), {'0', '1'})
        for proof in state['complete_build_proofs'].values():
            self.assertTrue(proof['admitted'])
            self.assertTrue(proof['whole_relation_unfiltered'])
            self.assertFalse(proof['predicted_complete_array_size_claimed'])

    def test_guarded_monitor_rejects_known_terminal_wal_overshoot_before_promotion(self):
        config = guarded_config()
        db = Mock()
        db.query.return_value = [{'bytes': O.GUARDED_INDEX_CAP + 8192}]
        with patch.object(O, 'resource_observation', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'terminal-page WAL envelope'):
                O.monitor_budget(config, db, 0, {}, O.time.monotonic())

    def test_owned_overcap_index_can_be_explicitly_rolled_back_without_promotion(self):
        config = guarded_config()
        old = original_catalog()
        now = copy.deepcopy(old)
        add_index(now, 0)
        state = {'intents': {'0': dict(intent(0)['0'], before={
                    'guarded_runtime_profile': guarded_profile()})}}
        db = Mock()
        db.query.return_value = [{'bytes': O.GUARDED_INDEX_CAP + 8192}]
        session = SimpleNamespace(statement=Mock(), scalar=Mock(return_value=False))
        flow = O.Workflow(db, session, {'original': old}, state, lambda s: None, config)
        flow.run_phase = Mock()
        with patch.object(O, 'catalog', side_effect=[now, old, old]):
            flow.rollback()
        flow.run_phase.assert_called_once_with(0, drop=True)
        self.assertFalse(state['complete_build_proofs']['0']['admitted'])
        self.assertEqual(state['status'], 'ROLLED_BACK_ORIGINAL_CATALOG_HELPERS_RETAINED')


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
