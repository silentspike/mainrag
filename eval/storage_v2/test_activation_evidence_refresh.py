"""Public fixtures for the same-set activation live-evidence refresh producer."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path.cwd()
SOURCE = Path(os.environ.get('ACTIVATION_REFRESH_CANDIDATE', str(ROOT / 'ops/storage-v2/activation-evidence-refresh.py')))
spec = importlib.util.spec_from_file_location('activation_refresh_fixture_candidate', SOURCE)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
OPS = R.operators(ROOT / 'ops/storage-v2')


class FakeObservations:
    """Explicit offline observations, never an alternate production backend."""
    def __init__(self, f):
        self.f = f;self.boot = 'public-fixture-boot';self.events = [];self.counters = {}
        self.o = SimpleNamespace(activation=OPS.activation,
            preflight=SimpleNamespace(evaluate=self.evaluate))
        self.changes = {};self.evaluate_count = 0
    def event(self, name):
        self.events.append(name);self.counters[name] = self.counters.get(name, 0) + 1
        self.f.wall += self.changes.get('seconds_per_call', 0)
        self.f.mono += self.changes.get('seconds_per_call', 0)
    def evaluate(self, *args, **kwargs):
        self.evaluate_count += 1
        return {'overall_status': 'PASS', 'checks': {'fixture': 'PASS'}}
    def check(self):
        self.event('check')
        R.require(self.f.wall < self.f.decision['valid_until_unix'], 'original deadline')
    def inventory(self, reader):
        self.event('inventory')
        value = {'inventory': {'sources': [{'source_id': c['source_id'], 'source_ref': str(self.counters['inventory']),
            'config_sha256': '1' * 64, 'generations': [{'status': 'release_candidate'}]} for c in self.f.candidates]},
            'audit': copy.deepcopy(self.f.audit)}
        if self.changes.get('candidate'):
            value['audit']['candidate_set'][0]['evidence_id'] += 1
        if self.changes.get('registry') and self.counters['inventory'] > 1:
            value['inventory']['sources'][0]['config_sha256'] = '2' * 64
        return value
    def package(self):
        self.event('package');value = copy.deepcopy(self.f.package)
        if self.changes.get('functions'):value['catalog']['functions'] = {'changed()': '0' * 64}
        if self.changes.get('pack') and self.counters['package'] > 1:value['metadata']['packs'][0]['stored_bytes'] += 1
        return value
    def maintenance(self, policy):
        self.event('maintenance')
        fields = ('active_writer_count', 'unknown_application_count', 'active_index_build_count', 'active_vacuum_count', 'active_backfill_count')
        result = {'units': {'discovery_failed': False, 'active_writer_count': 0, 'unknown_runtime_count': 0},
            'database': {'activity': {k: 0 for k in fields}, 'database_identity': self.f.database_identity},
            'client_version': '18.4'}
        if self.changes.get('writer'):result['database']['activity']['active_writer_count'] = 1
        return result
    def backup(self, identity):
        self.event('backup');result = {'status': 'PASS', 'evidence_sha256': self.f.cfg['backup_evidence']['sha256'], 'backup_schema_sha256': self.f.accepted['schema_sha256']}
        if self.changes.get('backup'):result['evidence_sha256'] = '0' * 64
        return result
    def watermarks(self, candidates, accepted):
        self.event('watermarks');result = copy.deepcopy(self.f.capture)
        if self.changes.get('watermarks'):result['watermarks'][0]['watermark_sha256'] = '0' * 64
        OPS.activation.verify_quiesced_watermarks(result, candidates, accepted['installed_binary_sha256'], int(self.f.wall))
        return result
    def legacy(self, baseline):
        self.event('legacy');result = {'legacy': copy.deepcopy(baseline), 'vector_metadata': {}}
        if self.changes.get('legacy'):result['legacy']['outbox_state']['pending'] = 1
        return result
    def resources(self, request, kernel, package):
        self.event('resources');result = copy.deepcopy(self.f.resources)
        if self.changes.get('resources'):result['generated_wal_bytes'] = 97 * R.GIB
        if self.changes.get('regression'):result['generated_wal_bytes'] = 1
        return result
    def code(self):
        self.event('code')
        return {'operator_commit_sha': 'b' * 40, 'tracked_clean': not self.changes.get('code')}


class ActivationEvidenceRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='activation-refresh-owned-fixture-');self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.binary = self.root / 'fixture-runtime';self.binary.write_bytes(b'public-fixture-runtime')
        self.wall = 2_000_000_000.;self.mono = 20_000.;self.db = {};self.index = 0;self.saved = {}
        self.database_identity = {'system_identifier': 'fixture-system', 'database': 'fixture_database', 'database_oid': '1'}
        self.functions = {'fixture_reader(bigint)': 'a' * 64, 'fixture_verifier(bigint)': 'b' * 64}
        authorities = {'fixture_reader(bigint)': {'owner': 'fixture'}, 'fixture_verifier(bigint)': {'owner': 'fixture'}}
        self.candidates = [dict(source_id=sid, candidate_generation_id=sid+100, candidate_generation_seq=1,
            candidate_commit_sha='c' * 40, source_watermark_sha256=R.digest({'fixture-watermark': sid}),
            item_count=1, adapter_profile_id='fixture.mutable.v1', evidence_id=sid+200,
            evidence_manifest_sha256='d' * 64, verification_manifest_sha256='e' * 64) for sid in (7, 8)]
        installation = self.put({'after': self.functions, 'after_authorities': authorities, 'schema_after': 181})
        fixture = self.put({'fixture': 'historical-native-proof-not-a-new-measurement', 'observed_at_unix': self.wall - 10000})
        writer = self.put({'function_identities': self.functions, 'schema_version': 181})
        runtime = self.put({'function_identities': self.functions, 'schema_version': 181, 'parent_runtime': writer,
            'reader181_installation': installation, 'binary_sha256': R.reference(self.binary)['sha256'], 'native_fixture_gate': fixture})
        reader = {'commit_sha': 'c' * 40, 'binary_sha256': R.reference(self.binary)['sha256'], 'installation_receipt_sha256': runtime['sha256']}
        self.audit = {'schema_version': 'mainrag.storage-v2.candidate-aggregate-audit.v2', 'persisted_candidate_set_complete': True,
            'persisted_gate_blockers': {}, 'candidate_set': self.candidates, 'candidate_set_sha256': R.digest(self.candidates),
            'source_count': 2, 'expected_reader_package': reader}
        audit_ref = self.put(self.audit)
        self.baseline = {'default_read_selector': 'legacy', 'postgresql_legacy_objects': [{'object_identity': 'public.files',
            'row_count': 2, 'definition_sha256': 'f' * 64}], 'qdrant_collections': [], 'qdrant_aliases': [],
            'outbox_state': {'pending': 0}, 'active_pointers': [{'source_id': 7, 'active_generation_id': None}, {'source_id': 8, 'active_generation_id': None}]}
        baseline = self.put(self.baseline)
        gates = {}
        for name in R.GATES:
            origin = baseline if name == 'legacy_state_readback' else self.put({'fixture': name, 'measured_at_unix': self.wall-10000})
            proof = self.put({'gate': name, 'status': 'PASS', 'candidate_set_sha256': self.audit['candidate_set_sha256'],
                'reader_installation_sha256': runtime['sha256'], 'observed_at_unix': self.wall-1000, 'raw_observation_references': [origin]})
            gates[name] = {'status': 'PASS', 'evidence_reference': proof, 'evidence_sha256': proof['sha256'], 'observed_at_unix': int(self.wall-1000)}
        self.accepted = {'schema_version': 'mainrag.storage-v2.aggregate-acceptance.v1', 'status': 'PASS', 'persisted_audit_sha256': audit_ref['sha256'],
            'candidate_set_sha256': self.audit['candidate_set_sha256'], 'source_count': 2, 'external_gates': gates,
            'schema_sha256': '1' * 64, 'backend_package_sha256': '2' * 64, 'installed_binary_sha256': reader['binary_sha256'],
            'code_commit_sha': 'c' * 40, 'preflight_operator_commit_sha': 'a' * 40,
            'current_source_watermarks': [{'source_id': c['source_id'], 'watermark_sha256': c['source_watermark_sha256'],
                'observed_at_unix': int(self.wall-1000)} for c in self.candidates], 'observed_at_unix': self.wall-1000}
        accepted_ref = self.put(self.accepted)
        self.capture = {'schema_version': 'mainrag.storage-v2.quiesced-watermarks.v1', 'status': 'PASS_LIVE_WATERMARKS_BEFORE_API_STOP',
            'candidate_set_sha256': self.audit['candidate_set_sha256'], 'installed_binary_sha256': reader['binary_sha256'],
            'reader_pid': 123, 'captured_at_unix': int(self.wall-10), 'source_count': 2,
            'watermarks': [{'source_id': c['source_id'], 'watermark_sha256': c['source_watermark_sha256'],
                'adapter_profile_id': c['adapter_profile_id'], 'item_count': c['item_count'], 'observed_at_unix': int(self.wall-10)} for c in self.candidates]}
        packs = [{'pack_id': 'fixture-pack', 'storage_key': 'fixture.pack', 'manifest_sha256': '3' * 64, 'stored_bytes': 10}]
        closure = self.put({'pack_root': str(self.root), 'packs': packs})
        backup_plan = self.put({'metadata': {'roles_sha256': '4' * 64}})
        backup = self.put({'function_map_sha256': '5' * 64, 'external_pack_closure': closure, 'plan': backup_plan})
        preflight = self.put({'mode': 'check', 'overall_status': 'PASS', 'checks': {'fixture': 'PASS'}, 'generated_at_unix': int(self.wall-20),
            'candidate': {'commit_sha': 'b' * 40, 'schema_sha256': '1' * 64, 'backend_lock_sha256': '2' * 64},
            'backup': {'evidence_sha256': backup['sha256']}})
        origin = self.put({'wal_start_lsn': '0/1', 'maximum_generated_wal_bytes': 96 * R.GIB})
        kernel = {'resource': {'kernel_pool_uuid': 'fixture-kernel', 'pool_uuid': 'fixture-pool', 'pool_size_bytes': 1000 * R.GIB,
            'generated_wal_bytes': 100, 'original_origin': origin}}
        binding = self.put({'actual_runtime': runtime, 'consumer_runtime': runtime, 'actual176_runtime': runtime, 'writer_runtime': writer})
        self.request = {'default_binding': binding, 'candidate_audit': audit_ref, 'persisted66_acceptance': accepted_ref,
            'kernel_readback': self.put(kernel), 'expected_pool_uuid': 'fixture-pool', 'boot_id': 'public-fixture-boot',
            'limits': {'maximum_work_seconds': 600, 'stop_tail_seconds': 225},
            'archive_command_sha256': '6' * 64, 'spool_root': str(self.root), 'global_wal_origin': origin, 'maximum_generated_wal_bytes': 96 * R.GIB}
        self.decision = {'actual_default_runtime': runtime, 'default_binding': binding, 'approved': True, 'root_reviewer_uid': 0,
            'independent_review_claimed': False, 'purpose': 'ATOMIC_ACTIVATION_AND_DEFAULT_SWITCH_ONLY', 'index_authority_inherited': False,
            'source_work_admitted': False, 'counters_reset': False, 'baseline_rebased': False, 'subject_payload_sha256': R.digest(self.request),
            'limits': self.request['limits'], 'boot_id': 'public-fixture-boot', 'at_unix': self.wall-30, 'at_monotonic': self.mono-30,
            'valid_until_unix': self.wall+500, 'valid_until_monotonic': self.mono+500, 'maximum_pool_percent': 95,
            'absolute_pool_ceiling_bytes': 950 * R.GIB, 'pool_size_bytes': 1000 * R.GIB, 'pool_uuid': 'fixture-pool',
            'minimum_physical_free_bytes': 42 * R.GIB, 'maximum_metadata_percent': 60, 'autoextend_credit_bytes': 0}
        decision_ref = self.put(self.decision);self.request['capacity_purpose_decision'] = decision_ref
        self.package = {'catalog': {'functions': self.functions, 'authorities': authorities}, 'metadata': {'database_identity': self.database_identity,
            'function_map_sha256': '5' * 64, 'roles_sha256': '4' * 64, 'packs': packs, 'source_writers': 0, 'active_conversions': 0},
            'binary_sha256': reader['binary_sha256']}
        self.resources = {'pool': {'uuid': 'fixture-kernel', 'size_bytes': 1000 * R.GIB, 'used_bytes': 800 * R.GIB,
            'metadata_used': 1, 'metadata_total': 100}, 'physical_free_bytes': 50 * R.GIB, 'generated_wal_bytes': 200,
            'spool_allocated_bytes': 100, 'wal_bytes': 100, 'wal_ready_bytes': 100, 'archive_command_sha256': '6' * 64}
        self.cfg = {'schema_version': R.CONFIG_SCHEMA, 'database': 'fixture_database', 'audit': audit_ref, 'accepted': accepted_ref, 'atomic_capacity_request': self.put(self.request),
            'capacity_decision': decision_ref, 'reader_receipt': runtime, 'reader_installation': installation,
            'preflight': preflight, 'preflight_policy': self.put({'backup_max_age_seconds': 129600}), 'legacy_baseline': baseline,
            'backup_evidence': backup, 'quiesced_watermarks': self.put(self.capture), 'immutable_watermarks': self.put({'fixture': 'bindings'}),
            'global_wal_origin': origin, 'operator_commit_sha': 'b' * 40, 'installed_binary': str(self.binary),
            'pack_root': str(self.root), 'spool_root': str(self.root), 'archive_command_sha256': '6' * 64}
        self.authorize()
        self.observe = FakeObservations(self)
    def put(self, value):
        self.index += 1;path = '/public-fixture/metadata-' + str(self.index) + '.json'
        self.db[path] = copy.deepcopy(value)
        return {'path': path, 'sha256': R.digest(value)}
    def read(self, ref):
        R.checked_ref(ref);value = self.db[ref['path']]
        R.require(R.digest(value) == ref['sha256'], 'protected fixture metadata changed')
        return copy.deepcopy(value)
    def authorize(self):
        self.cfg.pop('root_review', None)
        self.cfg['root_review'] = self.put({'status': 'ROOT_APPROVED_SAME_SET_ACTIVATION_EVIDENCE_REFRESH', 'approved': True,
            'root_reviewer_uid': 0, 'independent_review_claimed': False, 'payload_sha256': R.digest(self.cfg),
            'method': R.reference(SOURCE), 'at_unix': self.wall})
        self.cfg_ref = self.put(self.cfg)
    def save(self, label, value):
        self.assertNotIn(label, self.saved);self.saved[label] = copy.deepcopy(value)
        return self.put(value)
    def run_refresh(self):
        return R.refresh(self.cfg_ref, self.cfg, read=self.read, observe=self.observe, save=self.save,
                         now=lambda: self.wall, mono=lambda: self.mono)
    def test_backup_aged_acceptance_refreshed_without_changing_historical_proofs(self):
        old = copy.deepcopy(self.db)
        with self.assertRaisesRegex(RuntimeError, 'external aggregate gate'):
            OPS.activation.validate_acceptance(self.audit, self.cfg['audit']['sha256'], self.accepted, self.cfg['accepted']['sha256'],
                self.binary, self.read(self.cfg['preflight']), int(self.wall))
        result = self.read(self.run_refresh())
        self.assertEqual(result['code_commit_sha'], 'c' * 40)
        self.assertEqual(result['preflight_operator_commit_sha'], 'b' * 40)
        self.assertEqual(result['refresh_provenance']['previous_preflight_operator_commit_sha'], 'a' * 40)
        self.assertEqual(result['refresh_provenance']['previous_acceptance'], self.cfg['accepted'])
        self.assertEqual(result['candidate_set_sha256'], self.accepted['candidate_set_sha256'])
        for name in R.STATIC:self.assertEqual(result['external_gates'][name], self.accepted['external_gates'][name])
        for path, value in old.items():self.assertEqual(self.db[path], value)
        benchmark = self.read(result['external_gates']['benchmark_result']['evidence_reference'])
        self.assertEqual(benchmark['new_benchmark_measurements'], 0)
        self.assertTrue(benchmark['historical_measurements_reused']);self.assertTrue(benchmark['measurement_timestamps_not_rewritten'])
        self.assertEqual(benchmark['historical_measurement_evidence'], self.accepted['external_gates']['benchmark_result']['evidence_reference'])
        self.assertEqual(result['current_source_watermarks'], self.capture['watermarks'])
        lineage=dict(original_reference=self.cfg['accepted'], audit_reference=self.cfg['audit'], reader_reference=self.cfg['reader_receipt'],
            request_reference=self.cfg['atomic_capacity_request'], decision_reference=self.cfg['capacity_decision'],
            method_reference=R.reference(SOURCE), read=self.read)
        self.assertIs(R.verify_refresh_lineage(result, **lineage), result)
        foreign=copy.deepcopy(result);foreign['schema_sha256']='0'*64
        with self.assertRaisesRegex(RuntimeError,'refresh changed'):R.verify_refresh_lineage(foreign, **lineage)
        self.assertEqual(self.observe.evaluate_count, 1)
        self.assertEqual(self.observe.counters['inventory'], 2)
        OPS.activation.validate_acceptance(self.audit, self.cfg['audit']['sha256'], result, R.digest(result), self.binary,
                                          self.read(self.cfg['preflight']), int(self.wall))
    def test_each_real_live_gate_failure_produces_no_refresh_acceptance(self):
        for failure in ('functions', 'writer', 'backup', 'watermarks', 'legacy', 'resources', 'code'):
            with self.subTest(failure=failure):
                self.saved = {};self.observe = FakeObservations(self);self.observe.changes[failure] = True
                with self.assertRaises(RuntimeError):self.run_refresh()
                self.assertNotIn('aggregate-acceptance', self.saved)
    def test_candidate_registry_sealed_pack_drift_refused(self):
        for failure in ('candidate', 'registry', 'pack'):
            with self.subTest(failure=failure):
                self.saved = {};self.observe = FakeObservations(self);self.observe.changes[failure] = True
                with self.assertRaises(RuntimeError):self.run_refresh()
                self.assertNotIn('aggregate-acceptance', self.saved)
    def test_original_deadline_and96_percent_and_counter_regression_refused(self):
        self.wall += 501;self.mono += 501
        with self.assertRaises(RuntimeError):self.run_refresh()
        self.wall -= 501;self.mono -= 501
        bad = copy.deepcopy(self.decision);bad['maximum_pool_percent'] = 96
        with self.assertRaisesRegex(RuntimeError, 'original85or95'):
            R.atomic_budget(self.cfg, self.request, bad, wall=self.wall, mono=self.mono, boot=self.observe.boot)
        self.observe.changes['regression'] = True
        with self.assertRaisesRegex(RuntimeError, 'resource stock'):self.run_refresh()
    def test_no_retimestamp_acceptance_after_slow_metadata_work(self):
        self.observe.changes['seconds_per_call'] = 34
        with self.assertRaises(RuntimeError):self.run_refresh()
        self.assertNotIn('aggregate-acceptance', self.saved)
    def test_missing_or_changed_original_proof_rejects_before_observation(self):
        proof = self.accepted['external_gates']['per_class_and_aggregate_quality']['evidence_reference']
        self.db[proof['path']]['status'] = 'FAIL'
        with self.assertRaisesRegex(RuntimeError, 'protected fixture metadata'):self.run_refresh()
        self.assertEqual(self.observe.events, [])
    def test_live_producer_sql_is_readonly_and_rechecks_actual_metadata(self):
        seen = []
        def psql(database, statement, local, readonly):
            seen.append(statement);self.assertTrue(local and readonly);return json.dumps({'fixture': 1})
        activation = SimpleNamespace(psql=psql, require_stopped_api=lambda: None)
        obs = R.LiveObservations({'database': 'fixture'}, SimpleNamespace(activation=activation),
            {'valid_until_unix': time.time()+60, 'valid_until_monotonic': time.monotonic()+60})
        self.assertEqual(obs.sql('SELECT 1'), {'fixture': 1})
        self.assertIn('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY', seen[0])
        self.assertIn("statement_timeout=", seen[0]);self.assertTrue(seen[0].endswith(';COMMIT;'))
        self.assertNotIn('to_tsvector', R.CATALOG_SQL)
        self.assertNotRegex(R.CATALOG_SQL, r'source_id\s*=\s*[0-9]')
        self.assertIn('pg_ls_waldir()', R.CATALOG_SQL)
        # Exercise the exact existing backup validator ABI, including `now`.
        def validate(path, age, now, expected_identity=None):
            self.assertIsInstance(now, int);self.assertEqual(age, 129600)
            self.assertEqual(expected_identity, self.database_identity)
            return {'fixture': 'backup-ABI'}
        ops = SimpleNamespace(activation=activation, backup=SimpleNamespace(validate_evidence=validate))
        obs = R.LiveObservations({'database': 'fixture', 'backup_evidence': self.cfg['backup_evidence']}, ops,
            {'valid_until_unix': time.time()+60, 'valid_until_monotonic': time.monotonic()+60})
        self.assertEqual(obs.backup(self.database_identity), {'fixture': 'backup-ABI'})
        # Retained sources keep their own producer commits, not the reader's.
        candidates = copy.deepcopy(self.candidates);candidates[1]['candidate_commit_sha'] = 'd' * 40
        captured = []
        def capture(rows, operator, producer, **kwargs):
            self.assertIsNone(producer);captured.append(kwargs)
            return {'sources': []}, {}
        ops = SimpleNamespace(inventory=SimpleNamespace(INVENTORY_SQL=OPS.inventory.INVENTORY_SQL,capture=capture),
            audit=SimpleNamespace(audit=lambda *args: ({'persisted_candidate_set_complete': True, 'persisted_gate_blockers': {}}, {})))
        obs = R.LiveObservations({'audit': self.cfg['audit'], 'operator_commit_sha': 'b' * 40}, ops, {},
            read=lambda ref: {'candidate_set': candidates})
        obs.sql = lambda query: []
        obs.inventory({'commit_sha': 'c' * 40})
        self.assertEqual([r['candidate_commit_sha'] for r in captured[0]['candidate_commit_map']['sources']], ['c'*40,'d'*40])
        self.assertEqual(captured[0]['candidate_commit_map_sha256'], R.digest(captured[0]['candidate_commit_map']))
        stock = self.root / 'stock';stock.mkdir();leaf = stock / ('A'*24);leaf.write_bytes(b'fixture-stock')
        self.assertEqual(R.allocated_files(stock, wal=True), leaf.stat().st_blocks * 512)
    def test_legacy_observer_uses_supported_relations_and_rejects_selector_file(self):
        with self.assertRaisesRegex(RuntimeError, 'supported legacy relation scope'):
            R.legacy_query({'postgresql_legacy_objects': [{'object_identity': 'public.fixture_unreviewed'}]})
        self.assertIn('definition_sha256', R.legacy_query(self.baseline))
        file = self.root / 'selector.env';file.write_text('MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256=changed\n');file.chmod(0o600)
        switch = SimpleNamespace(ENV_FILE=self.root/'absent1', DROPIN=self.root/'absent2', UNIT='fixture.service',
            ENV_NAME='MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256', ACTIVE_COMMIT_ENV_NAME='MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA',
            systemctl=lambda *args: str(file)+' (ignore_errors=no)' if '--property=EnvironmentFiles' in args else '')
        cleanup = SimpleNamespace(qdrant_inventory=lambda *args: {'collections': [], 'aliases': []})
        obs = R.LiveObservations({'qdrant_url': 'http://127.0.0.1:1'}, SimpleNamespace(switch=switch, cleanup=cleanup), {})
        obs.sql = lambda text: {k: copy.deepcopy(v) for k,v in self.baseline.items() if k in ('postgresql_legacy_objects','outbox_state','active_pointers')}
        with self.assertRaisesRegex(RuntimeError, 'declared default selector'):obs.legacy(self.baseline)

if __name__ == '__main__':unittest.main()
