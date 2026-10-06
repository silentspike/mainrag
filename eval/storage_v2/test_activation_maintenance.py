"""Maintenance sequencing, stale evidence and cold default-switch regressions."""
import argparse
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


OP = load('prepared_activation_operator', ROOT / 'ops/storage-v2/activation-set.py')
SW = load('prepared_default_switch', ROOT / 'ops/storage-v2/default-read-switch.py')
EXISTING_OP = load('existing_activation_operator_tests', ROOT / 'eval/storage_v2/test_activation_set_operator.py')
EXISTING_OP.OPERATOR = OP
EXISTING_SW = load('existing_default_switch_tests', ROOT / 'eval/storage_v2/test_default_read_switch.py')
EXISTING_SW.SWITCH = SW


def stopped():
    return dict(LoadState='loaded', ActiveState='inactive', SubState='dead', MainPID='0', Result='success')


def fixture():
    now = int(time.time())
    candidates = [dict(source_id=1, source_watermark_sha256='a' * 64,
                       adapter_profile_id='fixture-v1', item_count=3)]
    receipt = dict(schema_version='mainrag.storage-v2.quiesced-watermarks.v1',
                   status='PASS_LIVE_WATERMARKS_BEFORE_API_STOP',
                   candidate_set_sha256=OP.sha256(OP.canonical(candidates)),
                   installed_binary_sha256='b' * 64, reader_pid=10, source_count=1,
                   captured_at_unix=now,
                   watermarks=[dict(source_id=1, watermark_sha256='a' * 64,
                                    adapter_profile_id='fixture-v1', item_count=3,
                                    observed_at_unix=now)])
    return now, candidates, receipt


class QuiescedActivationTests(unittest.TestCase):
    def test_exact_fresh_watermarks_match_final_set(self):
        now, candidates, receipt = fixture()
        OP.verify_quiesced_watermarks(receipt, candidates, 'b' * 64, now)

    def test_changed_stale_partial_or_wrong_package_fail(self):
        now, candidates, receipt = fixture()
        mutations = [
            lambda v: v.update(captured_at_unix=now - 301),
            lambda v: v.update(installed_binary_sha256='c' * 64),
            lambda v: v.update(candidate_set_sha256='c' * 64),
            lambda v: v.update(watermarks=[]),
            lambda v: v['watermarks'][0].update(observed_at_unix=now - 301),
            lambda v: v['watermarks'][0].update(watermark_sha256='c' * 64),
            lambda v: v['watermarks'][0].update(item_count=4),
            lambda v: v['watermarks'][0].update(adapter_profile_id='changed-v2'),
            lambda v: v.update(source_count=True),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = deepcopy(receipt)
                mutate(changed)
                with self.assertRaises(RuntimeError):
                    OP.verify_quiesced_watermarks(changed, candidates, 'b' * 64, now)

    def test_same_count_duplicate_source_is_rejected(self):
        now, candidates, receipt = fixture()
        candidates.append({**candidates[0], 'source_id': 2})
        receipt.update(candidate_set_sha256=OP.sha256(OP.canonical(candidates)), source_count=2)
        receipt['watermarks'].append(deepcopy(receipt['watermarks'][0]))
        with self.assertRaisesRegex(RuntimeError, 'observation differs'):
            OP.verify_quiesced_watermarks(receipt, candidates, 'b' * 64, now)

    def test_api_must_be_stopped_and_listener_must_refuse_connection(self):
        with patch.object(OP, 'api_unit_state', return_value=stopped()), \
             patch.object(OP.socket, 'create_connection', side_effect=ConnectionRefusedError):
            self.assertEqual(OP.require_stopped_api()['MainPID'], '0')
        with patch.object(OP, 'api_unit_state', return_value={**stopped(), 'ActiveState': 'active'}), \
             patch.object(OP.socket, 'create_connection') as connect:
            with self.assertRaisesRegex(RuntimeError, 'remain stopped'):
                OP.require_stopped_api()
            connect.assert_not_called()
        with patch.object(OP, 'api_unit_state', return_value=stopped()), \
             patch.object(OP.socket, 'create_connection', return_value=Mock()):
            with self.assertRaisesRegex(RuntimeError, 'live API listener'):
                OP.require_stopped_api()
        with patch.object(OP, 'api_unit_state', return_value=stopped()), \
             patch.object(OP.socket, 'create_connection', side_effect=socket.timeout):
            with self.assertRaisesRegex(RuntimeError, 'absence is unverified'):
                OP.require_stopped_api()

    def test_offline_verification_never_contacts_api(self):
        _, candidates, receipt = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'watermarks.json'
            OP.private_write(path, receipt)
            args = argparse.Namespace(quiesced_watermarks=path,
                                      quiesced_watermarks_sha256=OP.sha256(path.read_bytes()))
            with patch.object(OP, 'require_stopped_api') as stopped_gate, \
                 patch.object(OP, 'verify_current_api_watermarks') as api:
                OP.verify_activation_watermarks(args, candidates, dict(installed_binary_sha256='b' * 64))
            stopped_gate.assert_called_once()
            api.assert_not_called()

    def test_watermark_capture_binds_actual_reader_and_stops_on_restart(self):
        _, candidates, _ = fixture()
        audit = dict(persisted_candidate_set_complete=True, candidate_set=candidates,
                     candidate_set_sha256=OP.sha256(OP.canonical(candidates)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / 'api'
            binary.write_bytes(b'public fixture binary')
            audit_path = root / 'audit.json'
            OP.private_write(audit_path, audit)
            args = argparse.Namespace(audit=audit_path, audit_sha256=OP.sha256(audit_path.read_bytes()),
                                      installed_binary=binary, api_url='http://127.0.0.1:3001',
                                      api_token='public-test-token', output=root / 'capture.json')
            active = {**stopped(), 'ActiveState': 'active', 'SubState': 'running', 'MainPID': '10'}
            rows = fixture()[2]['watermarks']
            with patch.object(OP, 'api_unit_state', side_effect=[active, {**active, 'MainPID': '11'}]), \
                 patch.object(OP, 'running_binary_sha256', return_value=hashlib.sha256(binary.read_bytes()).hexdigest()), \
                 patch.object(OP, 'verify_current_api_watermarks', return_value=rows):
                with self.assertRaisesRegex(RuntimeError, 'restarted during capture'):
                    OP.capture_watermarks_command(args)
            self.assertFalse(args.output.exists())

    def test_cold_default_switch_installs_selector_before_starting_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = EXISTING_SW.DefaultReadSwitchTests()
            args, digest = helper.fixture(root)
            plan = json.loads(args.plan.read_text())
            plan['quiesced_watermarks_sha256'] = '9' * 64
            OP.private_write(args.plan, plan, replace=True)
            args.plan_sha256 = OP.sha256(args.plan.read_bytes())
            for path, hash_attribute in [(args.approval, 'approval_sha256'), (args.attempt, 'attempt_sha256')]:
                value = json.loads(path.read_text())
                value['plan_sha256'] = args.plan_sha256
                OP.private_write(path, value, replace=True)
                setattr(args, hash_attribute, OP.sha256(path.read_bytes()))

            def service_action(action, *arguments):
                if action == 'restart':
                    self.assertIn(digest, (root / 'selector.env').read_text())
                    self.assertIn('EnvironmentFile=', (root / 'unit.conf').read_text())
                return ''

            with patch.object(SW, 'DROPIN', root / 'unit.conf'), \
                 patch.object(SW, 'ENV_FILE', root / 'selector.env'), \
                 patch.object(SW, 'API_BINARY', root / 'mainrag-api'), \
                 patch.object(SW.OPERATOR, 'committed_readback', return_value={}), \
                 patch.object(SW.OPERATOR, 'verify_committed'), \
                 patch.object(SW.OPERATOR, 'api_unit_state', return_value=stopped()), \
                 patch.object(SW.OPERATOR, 'require_stopped_api') as boundary, \
                 patch.object(SW, 'read_service_state') as before_read, \
                 patch.object(SW, 'api_read_path') as before_api, \
                 patch.object(SW, 'verify_restarted_api', return_value=11) as new_api, \
                 patch.object(SW, 'service_binary_sha256', return_value=OP.sha256(b'reviewed-binary')), \
                 patch.object(SW, 'systemctl', side_effect=service_action) as service:
                result = SW.switch(args)
            self.assertTrue(result['cold_start_after_quiesced_activation'])
            self.assertEqual(result['status'], 'DEFAULT_SWITCHED_POST_INGEST_PENDING')
            boundary.assert_called_once()
            before_read.assert_not_called()
            before_api.assert_not_called()
            new_api.assert_called_once()
            service.assert_any_call('restart', SW.UNIT)



if __name__ == '__main__':
    unittest.main()
