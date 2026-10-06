"""Fresh kernel occupancy per batch, with periodic full LVM admission checks."""
import atexit
import ctypes
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time


def command(arguments):
    prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
    result = subprocess.run([*prefix, '/usr/sbin/dmsetup', *arguments],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('kernel thin-pool readback is unavailable')
    return result.stdout.strip()


class DmIoctl(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32 * 3), ('data_size', ctypes.c_uint32),
                ('data_start', ctypes.c_uint32), ('target_count', ctypes.c_uint32),
                ('open_count', ctypes.c_int32), ('flags', ctypes.c_uint32),
                ('event_nr', ctypes.c_uint32), ('padding', ctypes.c_uint32),
                ('dev', ctypes.c_uint64), ('name', ctypes.c_char * 128),
                ('uuid', ctypes.c_char * 129), ('data', ctypes.c_char * 7)]


class DmTarget(ctypes.Structure):
    _fields_ = [('start', ctypes.c_uint64), ('length', ctypes.c_uint64),
                ('status', ctypes.c_int32), ('next', ctypes.c_uint32),
                ('kind', ctypes.c_char * 16)]


def kernel_status(descriptor, uuid):
    # Linux dm-ioctl.h: DM_TABLE_STATUS, command 12. This helper exposes only
    # that read operation, bound to one UUID; no arbitrary ioctl/command input.
    if ctypes.sizeof(DmIoctl) != 312 or ctypes.sizeof(DmTarget) != 40:
        raise RuntimeError('unsupported device-mapper ABI')
    buffer = bytearray(65536)
    header = DmIoctl.from_buffer(buffer)
    header.version[:] = (4, 0, 0)
    header.data_size = len(buffer)
    header.data_start = ctypes.sizeof(DmIoctl)
    header.uuid = uuid.encode('ascii')
    operation = (3 << 30) | (ctypes.sizeof(DmIoctl) << 16) | (0xfd << 8) | 12
    fcntl.ioctl(descriptor, operation, buffer, True)
    if header.version[0] != 4 or header.target_count != 1 \
            or header.flags & ((1 << 0) | (1 << 1) | (1 << 8)) \
            or not header.flags & (1 << 5) or bytes(header.uuid).decode('ascii') != uuid \
            or not 312 <= header.data_start <= header.data_size - 40 \
            or header.data_size > len(buffer):
        raise RuntimeError('kernel pool identity, state or response bounds differ')
    target = DmTarget.from_buffer(buffer, header.data_start)
    if target.status != 0 or bytes(target.kind) != b'thin-pool':
        raise RuntimeError('kernel target type or status differs')
    start = header.data_start + ctypes.sizeof(DmTarget)
    end = buffer.find(0, start, header.data_size)
    if end < 0 or end - start > 4096:
        raise RuntimeError('kernel status text is unbounded')
    text = bytes(buffer[start:end]).decode('ascii')
    return f'{target.start} {target.length} thin-pool {text}'


def serve(uuid):
    if os.geteuid() != 0 or not re.fullmatch(r'LVM-[A-Za-z0-9]+-tpool', uuid):
        raise RuntimeError('privileged observer requires a bound LVM pool UUID')
    descriptor = os.open('/dev/mapper/control', os.O_RDONLY | os.O_CLOEXEC)
    try:
        sequence = 0
        while True:
            line = sys.stdin.buffer.readline(128)
            if not line:
                break  # Owner exit closes the pipe; no retained daemon.
            sequence += 1
            if line != f'{sequence}\n'.encode('ascii'):
                raise RuntimeError('observer request sequence differs')
            raw = kernel_status(descriptor, uuid)
            print(json.dumps(dict(sequence=sequence, status=raw)), flush=True)
    finally:
        os.close(descriptor)


class KernelObserver:
    def __init__(self, uuid):
        prefix = [] if os.geteuid() == 0 else ['sudo', '-n']
        self.process = subprocess.Popen([*prefix, '/usr/bin/python3', '-I', '-u', __file__, '--serve-uuid', uuid],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL,
                                        env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        self.sequence = 0
        atexit.register(self.close)

    def read(self):
        if self.process.poll() is not None:
            raise RuntimeError('owned kernel observer is not live')
        self.sequence += 1
        self.process.stdin.write(f'{self.sequence}\n'.encode('ascii'))
        self.process.stdin.flush()
        deadline = time.monotonic() + 10
        raw = bytearray()
        while not raw.endswith(b'\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise RuntimeError('owned kernel observer did not respond')
            part = os.read(self.process.stdout.fileno(), 4096)
            if not part or len(raw) + len(part) > 4096:
                raise RuntimeError('owned kernel observer response is invalid')
            raw.extend(part)
        value = json.loads(raw)
        if type(value.get('sequence')) is not int or value['sequence'] != self.sequence \
                or not isinstance(value.get('status'), str):
            raise RuntimeError('owned kernel observer response identity differs')
        return value['status']

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process.stdout:
            self.process.stdout.close()


def parse_status(raw, block_sectors, expected_pool_bytes):
    lines = raw.splitlines()
    if len(lines) != 1:
        raise RuntimeError('kernel thin-pool segment count differs')
    fields = lines[0].split()
    if len(fields) < 11 or fields[0] != '0' or fields[2] != 'thin-pool' \
            or fields[7] != 'rw' or fields[10] != '-':
        raise RuntimeError('kernel thin-pool is not healthy and writable')
    if not re.fullmatch(r'[0-9]+', fields[1]) or int(fields[1]) * 512 != expected_pool_bytes:
        raise RuntimeError('kernel thin-pool size differs from LVM admission')
    counts = []
    for field in fields[4:6]:
        if not re.fullmatch(r'[0-9]+/[0-9]+', field):
            raise RuntimeError('kernel thin-pool counters are invalid')
        used, total = map(int, field.split('/'))
        if total <= 0 or not 0 <= used <= total:
            raise RuntimeError('kernel thin-pool counter bounds differ')
        counts.append((used, total))
    (metadata_used, metadata_total), (data_used, data_total) = counts
    if data_total * block_sectors * 512 != expected_pool_bytes:
        raise RuntimeError('kernel thin-pool block geometry differs')
    return dict(data_percent=Decimal(data_used) * 100 / data_total,
                metadata_percent=Decimal(metadata_used) * 100 / metadata_total)


class LiveKernelCapacity:
    """Never cache occupancy; full configuration checks expire after 60 seconds.

    UUID selection binds every status read to the admitted pool. Mount device,
    block geometry, capacity, thresholds and configuration must remain unchanged.
    A failed refresh aborts before another producer request.
    """
    POLICY_SECONDS = 60
    IDENTITY_KEYS = ('mount_source', 'vg_name', 'thin_volume_name', 'pool_name',
                     'pool_size_bytes', 'maximum_pool_growth_bytes',
                     'maximum_data_percent', 'maximum_metadata_percent',
                     'autoextend_threshold_percent')

    def __init__(self, full_check, pack_root):
        self.full_check = full_check
        self.pack_root = Path(pack_root).resolve(strict=True)
        self.device = self.pack_root.stat().st_dev
        self.full = full_check()
        self.checked_at = time.monotonic()
        pool = self.full
        name = pool['vg_name'].replace('-', '--') + '-' + pool['pool_name'].replace('-', '--') + '-tpool'
        info = command(['info', '-c', '--noheadings', '--separator', ':', '-o', 'name,uuid', name]).split(':')
        if len(info) != 2 or info[0].strip() != name \
                or not re.fullmatch(r'LVM-[A-Za-z0-9]+-tpool', info[1].strip()):
            raise RuntimeError('kernel thin-pool UUID is invalid')
        self.uuid = info[1].strip()
        table = command(['table', '-u', self.uuid]).split()
        if len(table) < 8 or table[0] != '0' or table[2] != 'thin-pool' \
                or not re.fullmatch(r'[0-9]+', table[5]) or int(table[5]) <= 0:
            raise RuntimeError('kernel thin-pool table is invalid')
        self.block_sectors = int(table[5])
        self.observer = KernelObserver(self.uuid)

    def observe(self):
        if self.pack_root.stat().st_dev != self.device:
            raise RuntimeError('pack mount changed during legacy bootstrap')
        if time.monotonic() - self.checked_at >= self.POLICY_SECONDS:
            fresh = self.full_check()
            if any(fresh[key] != self.full[key] for key in self.IDENTITY_KEYS):
                raise RuntimeError('LVM identity or resource policy changed')
            self.full = fresh
            self.checked_at = time.monotonic()
        # Status is read afresh for EVERY producer batch, even within the
        # configuration-check interval. No last-known occupancy is reused.
        raw = self.observer.read()
        observed = parse_status(raw, self.block_sectors, self.full['pool_size_bytes'])
        projected = observed['data_percent'] + Decimal(self.full['maximum_pool_growth_bytes']) * 100 / self.full['pool_size_bytes']
        if projected > Decimal(str(self.full['maximum_data_percent'])) \
                or observed['metadata_percent'] >= Decimal(str(self.full['maximum_metadata_percent'])):
            raise RuntimeError('insufficient physical thin-pool headroom')
        return dict(self.full, data_percent_before_build=float(observed['data_percent']),
                    metadata_percent_before_build=float(observed['metadata_percent']),
                    projected_data_percent=float(projected),
                    capacity_provider='live_kernel_occupancy_and_periodic_lvm_policy',
                    kernel_pool_uuid=self.uuid,
                    configuration_check_age_seconds=time.monotonic() - self.checked_at)


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--serve-uuid':
        raise SystemExit(2)
    try:
        serve(sys.argv[2])
    except Exception:
        # No raw kernel errors, source content or private configuration output.
        raise SystemExit(1)
