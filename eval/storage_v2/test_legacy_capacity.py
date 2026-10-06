"""Reject stale configuration, unhealthy pools and unbound kernel responses."""
import ctypes
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

PATH = Path(__file__).resolve().parents[2] / 'ops/storage-v2/legacy_capacity.py'
SPEC = importlib.util.spec_from_file_location('tested_legacy_capacity', PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
UUID = 'LVM-abc-tpool'
POOL_BYTES = 100 * 256 * 512


def status(data=60, metadata=20):
    return f'0 25600 thin-pool 1 {metadata}/100 {data}/100 - rw discard_passdown queue_if_no_space - 1024'


def admission():
    return dict(mount_source='/dev/mapper/test', vg_name='vg', thin_volume_name='data',
                pool_name='pool', pool_size_bytes=POOL_BYTES, maximum_pool_growth_bytes=1024,
                maximum_data_percent=75, maximum_metadata_percent=60, autoextend_threshold_percent=80)


class KernelCapacityTests(unittest.TestCase):
    def fixture(self, root, callback=None):
        clock = [0.0]
        callback = callback or Mock(return_value=admission())
        observer = Mock()
        observer.read.return_value = status()
        with patch.object(M, 'command', side_effect=['vg-pool-tpool:' + UUID,
                         '0 25600 thin-pool 1:1 1:2 256 0 0']), \
                patch.object(M, 'KernelObserver', return_value=observer), \
                patch.object(M.time, 'monotonic', side_effect=lambda: clock[0]):
            guard = M.LiveKernelCapacity(callback, root)
        return guard, callback, observer, clock

    def test_every_batch_reads_current_occupancy_without_lvm_per_batch(self):
        with tempfile.TemporaryDirectory() as root:
            guard, full, observer, clock = self.fixture(root)
            observer.read.side_effect = [status(60), status(61), status(62)]
            with patch.object(M.time, 'monotonic', side_effect=lambda: clock[0]):
                values = [guard.observe() for _ in range(3)]
            self.assertEqual([v['data_percent_before_build'] for v in values], [60.0, 61.0, 62.0])
            self.assertEqual(observer.read.call_count, 3)
            self.assertEqual(full.call_count, 1)

    def test_current_data_or_metadata_exhaustion_blocks_next_batch(self):
        with tempfile.TemporaryDirectory() as root:
            for raw in [status(data=76), status(metadata=60)]:
                guard, full, observer, clock = self.fixture(root)
                observer.read.return_value = raw
                with self.subTest(raw=raw), self.assertRaisesRegex(RuntimeError, 'headroom'):
                    guard.observe()

    def test_full_configuration_refresh_failure_stops_before_status_read(self):
        with tempfile.TemporaryDirectory() as root:
            full = Mock(side_effect=[admission(), RuntimeError('configuration failed')])
            guard, full, observer, clock = self.fixture(root, full)
            clock[0] = 60
            with patch.object(M.time, 'monotonic', side_effect=lambda: clock[0]), \
                    self.assertRaisesRegex(RuntimeError, 'configuration failed'):
                guard.observe()
            observer.read.assert_not_called()

    def test_changed_identity_or_policy_is_rejected_on_refresh(self):
        with tempfile.TemporaryDirectory() as root:
            for key, value in [('pool_name', 'another'), ('maximum_data_percent', 70),
                               ('maximum_pool_growth_bytes', 2048)]:
                fresh = dict(admission(), **{key: value})
                guard, full, observer, clock = self.fixture(root, Mock(side_effect=[admission(), fresh]))
                clock[0] = 60
                with self.subTest(key=key), patch.object(M.time, 'monotonic', side_effect=lambda: clock[0]), \
                        self.assertRaisesRegex(RuntimeError, 'policy changed'):
                    guard.observe()
                observer.read.assert_not_called()

    def test_invalid_kernel_geometry_and_failure_states_are_rejected(self):
        bad = [status().replace('rw', 'ro'), status().replace('thin-pool', 'error'),
               status().replace('20/100', '101/100'), status().replace('60/100', '60/0'),
               status().replace('60/100', '60/101'), status().replace(' - 1024', ' needs_check 1024'),
               status().replace('25600', '25601'), status() + '\n' + status()]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                M.parse_status(raw, 256, POOL_BYTES)

    def test_ioctl_is_fixed_read_operation_and_checks_exact_uuid(self):
        calls = []
        def ioctl(descriptor, operation, buffer, mutate):
            calls.append((descriptor, operation, mutate))
            header = M.DmIoctl.from_buffer(buffer)
            self.assertEqual(bytes(header.uuid), UUID.encode())
            header.target_count = 1; header.flags = 1 << 5
            target = M.DmTarget.from_buffer(buffer, 312)
            target.kind = b'thin-pool'; target.length = 25600
            text = status().split('thin-pool ', 1)[1].encode() + b'\0'
            buffer[352:352 + len(text)] = text
            header.data_size = 352 + len(text)
        with patch.object(M.fcntl, 'ioctl', side_effect=ioctl):
            self.assertEqual(M.kernel_status(99, UUID), status())
        self.assertEqual(calls, [(99, (3 << 30) | (312 << 16) | (0xfd << 8) | 12, True)])
        def changed_uuid(descriptor, operation, buffer, mutate):
            ioctl(descriptor, operation, buffer, mutate)
            M.DmIoctl.from_buffer(buffer).uuid = b'LVM-other-tpool'
        with patch.object(M.fcntl, 'ioctl', side_effect=changed_uuid), self.assertRaises(RuntimeError):
            M.kernel_status(99, UUID)

    def test_dead_or_unresponsive_owned_observer_cannot_supply_admission(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(M.subprocess, 'Popen', return_value=process), patch.object(M.atexit, 'register'):
            observer = M.KernelObserver(UUID)
        with self.assertRaisesRegex(RuntimeError, 'not live'):
            observer.read()
        process.poll.return_value = None
        with patch.object(M.select, 'select', return_value=([], [], [])), \
                self.assertRaisesRegex(RuntimeError, 'did not respond'):
            observer.read()


if __name__ == '__main__':
    unittest.main()
