"""External retirement protocol: exact targets, interrupted dispatch and drift."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eval.storage_v2.test_cleanup_plan import catalog_fixture

SPEC = importlib.util.spec_from_file_location('cleanup_external_tests',
    Path(__file__).resolve().parents[2]/'ops/storage-v2/cleanup-external.py')
E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E)


def component(identity='fixture.service', role='producer'):
    properties = {k: '' for k in E.P.PROPERTIES}
    properties.update(Id=identity, LoadState='loaded', ActiveState='active', SubState='running',
                      UnitFileState='enabled', MainPID='123', ControlGroup='/fixture')
    return {'kind': 'systemd_unit', 'identity': identity, 'role': role, 'properties': properties,
            'definitions': [{'path': '/fixture', 'device': 1, 'inode': 2, 'size': 3,
                             'mtime_ns': 4, 'sha256': 'a'*64}], 'pending_job': False}


def prepared(components=None, retire_backend=False):
    catalog = catalog_fixture()
    catalog['active_pointer_count'] = 1
    external = {'schema_version': 'mainrag.storage-v2.cleanup-external-inventory.v1',
        'qdrant': {'schema_version': 'mainrag.storage-v2.cleanup-qdrant.v1',
            'consistency': 'TWO_LIST_READBACKS_NOT_ATOMIC',
            'collections': [{'name': 'fixture_old', 'exact_point_count': 1, 'status': 'green',
                             'config_sha256': 'a'*64, 'detail_sha256': 'b'*64},
                            {'name': 'fixture_keep', 'exact_point_count': 2, 'status': 'green',
                             'config_sha256': 'c'*64, 'detail_sha256': 'd'*64}],
            'aliases': [{'alias_name': 'fixture_old_alias', 'collection_name': 'fixture_old'},
                        {'alias_name': 'fixture_keep_alias', 'collection_name': 'fixture_keep'}]},
        'components': components or []}
    if retire_backend:
        external['qdrant']['collections'] = external['qdrant']['collections'][:1]
        external['qdrant']['aliases'] = external['qdrant']['aliases'][:1]
    inventory = {'schema_version': 'mainrag.storage-v2.cleanup-catalog.v1', 'status': 'OBSERVED_ONLY',
                 'catalog': catalog, 'before_state_sha256': E.A.digest(catalog), 'operator_sha256': 'e'*64,
                 'qdrant': external['qdrant'], 'components': external['components']}
    decisions = {r['key']: {'key': r['key'], 'disposition': 'DELETE' if
        r['kind'] in ('systemd_unit', 'docker_container') and r['observed']['role'] != 'retained' or
        r['observed'].get('name') == 'fixture_old' or r['observed'].get('alias_name') == 'fixture_old_alias'
        else 'KEEP', 'reason': 'public fixture only', 'authority': 'fixture'}
        for r in E.M.observed_objects(inventory)}
    draft = E.M.draft(inventory, 'c'*64, decisions)
    storage = {'root': '/fixture', 'device': 1, 'inode': 2, 'allocated_bytes': 4096,
               'filesystem_available_bytes': 8192}
    return inventory, draft, E.build_plan(inventory, 'c'*64, draft, 'd'*64, 'e'*64, 'f'*64,
                                          storage, 'http://127.0.0.1:6333')


class CleanupExternalTests(unittest.TestCase):
    def test_producer_alias_collection_backend_order_and_activator_closure(self):
        producer = component('fixture-writer.timer')
        backend = component('fixture-vector.service', 'qdrant_backend')
        backend['properties']['TriggeredBy'] = producer['identity']
        _, _, plan = prepared([backend, producer], retire_backend=True)
        self.assertEqual([r['kind'] for r in plan['targets']],
                         ['systemd_unit', 'qdrant_alias', 'qdrant_collection', 'systemd_unit'])
        with self.assertRaisesRegex(RuntimeError, 'activator'):
            prepared([backend], retire_backend=True)
        with self.assertRaisesRegex(RuntimeError, 'retained collections'):
            prepared([component('fixture-vector.service', 'qdrant_backend')])
        producer['pending_job'] = True
        with self.assertRaisesRegex(RuntimeError, 'transition'):
            prepared([producer])

    def test_retained_alias_and_tampered_dispositions_are_rejected(self):
        inventory, draft, plan = prepared()
        objects = E.M.observed_objects(inventory)
        decisions = {r['key']: {'key': r['key'], 'disposition': 'DELETE' if r['kind'] == 'qdrant_collection'
                               and r['observed']['name'] == 'fixture_old' else 'KEEP',
                               'reason': 'fixture', 'authority': 'fixture'} for r in objects}
        draft = E.M.draft(inventory, 'c'*64, decisions)
        with self.assertRaisesRegex(RuntimeError, 'retained alias'):
            E.build_plan(inventory, 'c'*64, draft, 'd'*64, 'e'*64, 'f'*64,
                         plan['vector_space_before'], plan['qdrant_origin'])
        draft['objects'][0]['observed_sha256'] = '0'*64
        with self.assertRaisesRegex(RuntimeError, 'dispositions differ'):
            E.build_plan(inventory, 'c'*64, draft, 'd'*64, 'e'*64, 'f'*64,
                         plan['vector_space_before'], plan['qdrant_origin'])

    def test_complete_dispatch_keeps_other_objects_and_create_only_attempt(self):
        _, _, plan = prepared([component()])
        current = copy.deepcopy(plan['external_before'])
        calls, guards = [], []
        def mutate(target):
            nonlocal current
            calls.append(target['kind'])
            current = E.expected_after(current, target)
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary)/'attempt'
            result = E.apply_steps(plan, '1'*64, attempt, lambda: current,
                                   lambda: guards.append(True), mutate)
            self.assertEqual(calls, ['systemd_unit', 'qdrant_alias', 'qdrant_collection'])
            self.assertGreater(len(guards), len(calls))
            self.assertFalse(result['physical_reclamation_verified'])
            self.assertEqual(current['qdrant']['collections'][0]['name'], 'fixture_keep')
            self.assertTrue(E.reconcile(plan, '1'*64, attempt, lambda: current)['last_step_confirmed'])
            with self.assertRaises((RuntimeError, FileExistsError)):
                E.apply_steps(plan, '1'*64, attempt, lambda: current, lambda: None, mutate)
            self.assertEqual(len(calls), 3)

    def test_unknown_committed_outcome_is_reconciled_without_repeat(self):
        _, _, plan = prepared()
        current = copy.deepcopy(plan['external_before'])
        calls = []
        def mutate(target):
            nonlocal current
            calls.append(target['key'])
            current = E.expected_after(current, target)
            raise RuntimeError('response lost after commit')
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary)/'attempt'
            with self.assertRaisesRegex(RuntimeError, 'response lost'):
                E.apply_steps(plan, '1'*64, attempt, lambda: current, lambda: None, mutate)
            result = E.reconcile(plan, '1'*64, attempt, lambda: current)
            self.assertTrue(result['last_step_confirmed'])
            self.assertFalse(result['automatic_retry_allowed'])
            self.assertFalse(result['mutation_performed'])
            self.assertEqual(len(calls), 1)
            (attempt/'00001.json').unlink()
            self.assertFalse(E.reconcile(plan, '1'*64, attempt, lambda: current)['last_step_confirmed'])

    def test_retained_drift_stops_later_dispatch_and_journal_tampering_fails(self):
        _, _, plan = prepared()
        current = copy.deepcopy(plan['external_before'])
        calls = []
        def mutate(target):
            nonlocal current
            calls.append(target['key'])
            current = E.expected_after(current, target)
            current['qdrant']['collections'][1]['exact_point_count'] += 1
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary)/'attempt'
            with self.assertRaisesRegex(RuntimeError, 'retained state differs'):
                E.apply_steps(plan, '1'*64, attempt, lambda: current, lambda: None, mutate)
            self.assertEqual(len(calls), 1)
            self.assertFalse(E.reconcile(plan, '1'*64, attempt, lambda: current)['last_step_confirmed'])
            path = attempt/'00001.json'
            row = json.loads(path.read_text());row['previous_sha256'] = '0'*64
            path.write_text(json.dumps(row));path.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, 'chain'):
                E.reconcile(plan, '1'*64, attempt, lambda: current)

    def test_native_guard_rejects_uncommitted_parent_changed_roots_and_writers(self):
        _, _, plan = prepared()
        parent = {'pointer_set_sha256': plan['pointer_set_sha256'],
                  'runtime_package_sha256': plan['runtime_package_sha256'],
                  'result': {'status': 'DB_COMMITTED_POSTCHECK_PENDING'}}
        lease = unittest.mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);s = root.stat()
            plan['vector_space_before'].update(root=str(root), device=s.st_dev, inode=s.st_ino)
            with patch.object(E.A, 'validate_runtime'), patch.object(E.A, 'receipt', return_value=parent), \
                    patch.object(E.C, 'catalog', return_value=plan['catalog']), patch.object(E.A, 'psql', return_value='f'):
                E.native_guard(plan, {}, 'fixture', False, lease)
                with patch.object(E.A, 'receipt', return_value=None), self.assertRaisesRegex(RuntimeError, 'committed'):
                    E.native_guard(plan, {}, 'fixture', False, lease)
                with patch.object(E.C, 'catalog', return_value={**plan['catalog'], 'building_run_count': 1}), \
                        self.assertRaisesRegex(RuntimeError, 'writers drifted'):
                    E.native_guard(plan, {}, 'fixture', False, lease)
                with patch.object(E.A, 'psql', return_value='t'), self.assertRaisesRegex(RuntimeError, 'reader/writer'):
                    E.native_guard(plan, {}, 'fixture', False, lease)
        self.assertGreater(lease.check.call_count, 0)

    def test_backend_observation_needs_empty_prior_api_exact_stop_and_tcp_refusal(self):
        _, _, plan = prepared([component('fixture-vector.service', 'qdrant_backend')], retire_backend=True)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);plan['vector_space_before']['root'] = str(root)
            prior = copy.deepcopy(plan['external_before'])
            retired = {'kind': 'systemd_unit', 'identity': 'fixture-vector.service', 'role': 'qdrant_backend', 'retired': True}
            observer = E.LiveObserver(plan, None, False, prior)
            with patch.object(E.P, 'observe', return_value=retired), self.assertRaisesRegex(RuntimeError, 'empty'):
                observer()
            prior['qdrant']['collections'] = [];prior['qdrant']['aliases'] = []
            observer = E.LiveObserver(plan, None, False, prior)
            with patch.object(E.P, 'observe', return_value=retired), \
                    patch.object(E.socket, 'create_connection', side_effect=ConnectionRefusedError):
                self.assertEqual(observer()['qdrant'], prior['qdrant'])
                self.assertTrue(observer.backend_stop_proven)
                (root/'collections').mkdir();(root/'collections'/'unplanned').mkdir()
                with self.assertRaisesRegex(RuntimeError, 'storage entries'):
                    observer()
            with patch.object(E.P, 'observe', return_value=retired), \
                    patch.object(E.socket, 'create_connection', side_effect=TimeoutError), \
                    self.assertRaisesRegex(RuntimeError, 'not proof'):
                observer()

    def test_component_identity_secret_redaction_and_docker_volume_retention(self):
        for identity in ('fixture', '--all', 'a'*12, 'fixture.service;touch bad'):
            with self.subTest(identity=identity), self.assertRaisesRegex(RuntimeError, 'canonical'):
                E.P.validate_specs({'schema_version': 'mainrag.storage-v2.cleanup-components-config.v1',
                                   'components': [{'kind': 'docker_container', 'identity': identity, 'role': 'producer'}]})
        spec = {'kind': 'docker_container', 'identity': 'a'*64, 'role': 'legacy_backend'}
        state = {k: False for k in ('Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead')}
        state.update(Status='exited', Pid=0, StartedAt='fixture', FinishedAt='fixture',
                     Health={'Log': [{'Output': 'fixture_secret_must_not_escape'}]})
        raw = [{'Id': 'a'*64, 'Image': 'fixture-image', 'State': state,
                'Config': {'Env': ['fixture_secret_must_not_escape']}, 'HostConfig': {}, 'Mounts': []}]
        with patch.object(E.P, 'command', return_value=subprocess.CompletedProcess([], 0, json.dumps(raw), '')):
            before = E.P.observe(spec)
            self.assertNotIn('fixture_secret_must_not_escape', json.dumps(before))
        with patch.object(E.P, 'observe', side_effect=[before, {**spec, 'absent': True}]), \
                patch.object(E.P, 'command') as command:
            E.P.retire(spec, before, '1'*64)
            self.assertEqual(command.call_args.args[0], ['docker', 'container', 'rm', '--force', '--', 'a'*64])
            self.assertNotIn('--volumes', command.call_args.args[0])

    def test_vector_measurement_deduplicates_hardlinks_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary);file = root/'point';file.write_bytes(b'x'*8192)
            before = E.vector_space(root);os.link(file, root/'same-point')
            self.assertEqual(E.vector_space(root)['allocated_bytes'], before['allocated_bytes'])
            (root/'unsafe').symlink_to(file)
            with self.assertRaisesRegex(RuntimeError, 'symlink'):
                E.vector_space(root)

    def test_timer_observation_accepts_absent_service_process_properties_only(self):
        spec = {'kind': 'systemd_unit', 'identity': 'fixture.timer', 'role': 'producer'}
        values = {k: '' for k in E.P.PROPERTIES if k not in
                  ('MainPID', 'ControlGroup', 'ExecMainStartTimestampMonotonic')}
        values.update(Id='fixture.timer', LoadState='loaded', ActiveState='active',
                      SubState='waiting', UnitFileState='enabled')
        output = '\n'.join(k+'='+v for k, v in values.items())
        result = subprocess.CompletedProcess([], 0, output, '')
        with patch.object(E.P, 'command', side_effect=[result, subprocess.CompletedProcess([], 0, '', '')]):
            observed = E.P.observe(spec)
        self.assertEqual(observed['properties']['MainPID'], '0')
        del values['ActiveState']
        with patch.object(E.P, 'command', return_value=subprocess.CompletedProcess([], 0,
                '\n'.join(k+'='+v for k,v in values.items()), '')), self.assertRaisesRegex(RuntimeError, 'incomplete'):
            E.P.observe(spec)

    def test_recreated_container_identity_is_not_hidden_by_original_absence(self):
        _, _, plan = prepared()
        old = {'kind': 'docker_container', 'identity': 'a'*64, 'role': 'producer',
               'absent': False, 'state': {'Pid': 1}, 'configuration_sha256': 'b'*64}
        plan['external_before']['components'] = [old]
        plan['external_before']['container_ids'] = ['a'*64, 'c'*64]
        target = {'kind': 'docker_container', 'key': E.M.object_key('docker_container', ('a'*64,)), 'observed': old}
        plan['targets'] = [target]
        current = copy.deepcopy(plan['external_before'])
        def mutate(unused):
            nonlocal current
            current = E.expected_after(current, target)
            current['container_ids'].append('d'*64)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'retained state differs'):
                E.apply_steps(plan, '1'*64, Path(temporary)/'attempt', lambda: current, lambda: None, mutate)
        self.assertEqual(E.expected_after(plan['external_before'], target)['container_ids'], ['c'*64])


if __name__ == '__main__':
    unittest.main()
