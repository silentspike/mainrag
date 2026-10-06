#!/usr/bin/env python3
"""Bounded, lossless posting compaction; committed receipts precede progress.

The database codec preserves literal terms and frequencies. Status is read-only;
advancing, publishing, rollback and retirement are distinct owner operations.
Neither logical byte counts nor relation allocation imply physical reclamation.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Callable

HERE = Path(__file__).resolve().parent
HEX = re.compile(r'[0-9a-f]{64}\Z')
STATE_COLUMNS = ('document_id', 'materialization_sha256', 'phase', 'last_term_sha256',
                 'next_block', 'posting_count', 'pairs_sha256', 'complete', 'retired')
MINIMUM_RESERVE = 42 * 1024 ** 3
WAL_STOP_BYTES = 24 * 1024 ** 3


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha_bytes(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError('a lowercase SHA-256 identity is required')
    return bytes.fromhex(value)


def file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def operator_sha():
    paths = (Path(__file__), HERE / 'legacy_capacity.py', HERE / 'release-candidate.py',
             HERE.parent.parent / 'migrations/157_storage_v2_lossless_posting_compaction.sql')
    return hashlib.sha256(json.dumps({p.name: file_sha(p) for p in paths},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_read(path):
    path = Path(path)
    owners = {os.geteuid(), 0, int(os.environ.get('SUDO_UID', '-1'))}
    parent = path.parent.resolve(strict=True).stat()
    if parent.st_mode & 0o077 or parent.st_uid not in owners:
        raise RuntimeError('input directory must remain private and operator-owned')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o077 \
                or before.st_uid not in owners or before.st_size > 8 * 1024 ** 2:
            raise RuntimeError('input must be a bounded private operator-owned regular file')
        raw = stream.read(8 * 1024 ** 2 + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > 8 * 1024 ** 2 or (before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino,
            after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise RuntimeError('protected input changed during read')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise RuntimeError('protected input must be an object')
    return value, hashlib.sha256(raw).hexdigest()


def literal(value):
    if value is None:
        return 'NULL'
    if isinstance(value, bytes):
        return "decode('" + value.hex() + "','hex')"
    if type(value) is int:
        return str(value)
    if isinstance(value, str) and '\x00' not in value:
        return "'" + value.replace("'", "''") + "'"
    raise ValueError('unsupported SQL parameter')


class Database:
    """One psql transaction per operation; no credential-bearing connection URL."""
    def __init__(self, database, user_id, *, socket=None, privileged=False):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', database):
            raise ValueError('a local database name is required')
        self.database = database
        self.user_id = str(uuid.UUID(user_id))
        self.command = (['sudo', '-n', '-u', 'postgres'] if privileged else []) + [
            'psql', '-X', '--no-psqlrc', '-qAt', '-v', 'ON_ERROR_STOP=1', '-d', database]
        if socket is not None:
            self.command += ['--host', str(socket)]

    def query(self, query, arguments=(), *, readonly=True, timeout=30):
        parts = query.split('%s')
        if len(parts) != len(arguments) + 1:
            raise ValueError('SQL parameter count differs')
        statement = parts[0] + ''.join(literal(v) + suffix for v, suffix in zip(arguments, parts[1:]))
        sql = ('BEGIN' + (' READ ONLY' if readonly else '') + ';'
               f"SET LOCAL app.user_id={literal(self.user_id)};"
               f"SET LOCAL statement_timeout={literal(str(timeout) + 's')};"
               "SET LOCAL lock_timeout='3s';SET LOCAL application_name='posting-compaction';"
               "SELECT coalesce(jsonb_agg(to_jsonb(result)),'[]'::jsonb) FROM (" + statement + ') result;COMMIT;')
        result = subprocess.run(self.command, input=sql, capture_output=True, text=True,
                                timeout=timeout + 10)
        if result.returncode:
            # Server details may contain private terms; never expose raw stderr.
            raise RuntimeError('posting operation failed or its outcome is unknown; reconcile the same database receipt')
        return json.loads(result.stdout.strip())


@dataclass(frozen=True)
class DocumentPin:
    document_id: int
    materialization_sha256: str

    def __post_init__(self):
        if type(self.document_id) is not int or self.document_id <= 0:
            raise ValueError('a positive exact document identity is required')
        sha_bytes(self.materialization_sha256)


class PostingConversion:
    """Inject the runner's actual identity/resource/acceptance gates and event sink.

    Gates raise on failure. Reconcile status after any unknown acknowledgement;
    receipts identify the same manifest/document/block, never a new retry ID.
    Importing this module does not connect, install, mutate or run a benchmark.
    """
    def __init__(self, database, manifest_sha256, *, identity_gate: Callable,
                 resource_gate: Callable, acceptance_gate: Callable,
                 durable_event: Callable, max_batch_bytes=8 * 1024 ** 2):
        self.database = database
        self.manifest_sha256 = manifest_sha256
        self.manifest = sha_bytes(manifest_sha256)
        if type(max_batch_bytes) is not int or not 1 <= max_batch_bytes <= 64 * 1024 ** 2:
            raise ValueError('batch term bytes must be bounded to at most 64 MiB')
        self.identity_gate = identity_gate
        self.resource_gate = resource_gate
        self.acceptance_gate = acceptance_gate
        self.durable_event = durable_event
        self.max_batch_bytes = max_batch_bytes

    def status(self, pin):
        rows = self.database.query("""SELECT document_id,encode(materialization_sha256,'hex') materialization_sha256,
            phase,encode(last_term_sha256,'hex') last_term_sha256,next_block,posting_count,
            encode(pairs_sha256,'hex') pairs_sha256,complete,retired,encode(manifest_sha256,'hex') manifest_sha256
            FROM storage_v2_posting_conversion WHERE document_id=%s""", (pin.document_id,))
        if not rows:
            return None
        state = rows[0]
        if state['manifest_sha256'] != self.manifest_sha256 \
                or state['materialization_sha256'] != pin.materialization_sha256:
            raise RuntimeError('retained conversion belongs to a different manifest/materialization')
        return state

    def status_many(self, pins):
        """Read metadata in bounded groups rather than spawn one client per ID."""
        found = {}
        for offset in range(0, len(pins), 512):
            subset = pins[offset:offset + 512]
            requested = ','.join(str(pin.document_id) for pin in subset)
            rows = self.database.query("""SELECT document_id,encode(materialization_sha256,'hex') materialization_sha256,
                phase,encode(last_term_sha256,'hex') last_term_sha256,next_block,posting_count,
                encode(pairs_sha256,'hex') pairs_sha256,complete,retired,encode(manifest_sha256,'hex') manifest_sha256
                FROM storage_v2_posting_conversion WHERE document_id IN (""" + requested + ')')
            identities = {pin.document_id: pin.materialization_sha256 for pin in subset}
            for state in rows:
                if state['manifest_sha256'] != self.manifest_sha256 \
                        or state['materialization_sha256'] != identities[state['document_id']]:
                    raise RuntimeError('retained conversion belongs to a different manifest/materialization')
                found[state['document_id']] = state
        return [found.get(pin.document_id) for pin in pins]

    def _gates(self, pin):
        self.identity_gate(self.manifest_sha256, pin)
        self.resource_gate(self.manifest_sha256, pin, self.max_batch_bytes)

    def prepare(self, pin):
        self._gates(pin)
        self.database.query('SELECT (storage_v2_prepare_posting_conversion(%s,%s,%s)).document_id',
                            (pin.document_id, sha_bytes(pin.materialization_sha256), self.manifest), readonly=False)
        return self.status(pin)

    def advance(self, pin):
        state = self.status(pin)
        if state is None:
            state = self.prepare(pin)
        if state['complete'] or state['retired'] or state['phase'] != 'FLAT':
            return state
        self._gates(pin)
        operation = hashlib.sha256(
            f"{self.manifest_sha256}:{pin.document_id}:{state['next_block']}".encode('ascii')).hexdigest()
        receipt = self.database.query("""SELECT r.document_id,r.block_order,r.term_count,r.term_bytes,
             r.pairs_sha256 FROM
             storage_v2_copy_posting_conversion_group(%s,%s,%s,%s,%s) r""",
            (pin.document_id, self.manifest, state['next_block'],
             None if state['last_term_sha256'] is None else sha_bytes(state['last_term_sha256']),
             self.max_batch_bytes), readonly=False)[0]
        self.durable_event(dict(status='COMMITTED', kind='POSTING_BATCH', operation_sha256=operation,
                                **receipt, physical_reclaim_bytes=None))
        return self.status(pin)

    def publish(self, pin):
        state = self.status(pin)
        if state is None or not state['complete'] or state['retired']:
            raise RuntimeError('a complete, unretired representation is required')
        if state['phase'] == 'COMPACT':
            return state
        self._gates(pin)
        self.acceptance_gate(self.manifest_sha256, pin, state)
        self.database.query('SELECT (storage_v2_publish_posting_conversion(%s,%s,%s,%s)).document_id',
                            (pin.document_id, self.manifest, state['posting_count'], sha_bytes(state['pairs_sha256'])),
                            readonly=False)
        self.durable_event(dict(status='COMMITTED', kind='POSTING_PUBLISHED', document_id=pin.document_id,
                                posting_count=state['posting_count'], pairs_sha256=state['pairs_sha256'],
                                physical_reclaim_bytes=None))
        return self.status(pin)

    def restore_flat_visibility(self, pin):
        self._gates(pin)
        self.database.query('SELECT (storage_v2_restore_flat_posting_visibility(%s,%s)).document_id',
                            (pin.document_id, self.manifest), readonly=False)
        self.durable_event(dict(status='COMMITTED', kind='FLAT_VISIBILITY_RESTORED',
                                document_id=pin.document_id, physical_reclaim_bytes=None))
        return self.status(pin)


def validate_manifest(value):
    if value.get('schema_version') != 'mainrag.storage-v2.posting-compaction.v1' \
            or value.get('preserve_all_generations') is not True \
            or not isinstance(value.get('authority'), str) or not value['authority'].strip() \
            or not isinstance(value.get('documents'), list) or not value['documents'] \
            or len(value['documents']) > 100000:
        raise RuntimeError('complete authorized retained-state manifest required')
    for name in ('operator_sha256', 'runtime_binary_sha256', 'generation_state_sha256',
                 'pointer_state_sha256', 'catalog_contract_sha256'):
        sha_bytes(value.get(name))
    pins = [DocumentPin(**v) for v in value['documents']]
    if len({p.document_id for p in pins}) != len(pins):
        raise RuntimeError('manifest repeats a document')
    for name in ('max_batch_bytes', 'maximum_batch_growth_bytes', 'minimum_free_bytes'):
        if type(value.get(name)) is not int or value[name] <= 0:
            raise RuntimeError('explicit reviewed resource bounds required')
    if not 1 <= value['max_batch_bytes'] <= 64 * 1024 ** 2 \
            or value['maximum_batch_growth_bytes'] < value['max_batch_bytes'] \
            or value['minimum_free_bytes'] < MINIMUM_RESERVE:
        raise RuntimeError('manifest weakens batch or reserve bounds')
    return pins


def state_identities(database):
    return database.query("""SELECT
        encode(sha256(convert_to(coalesce((SELECT jsonb_agg(to_jsonb(g) ORDER BY id)::text
          FROM source_generation g),'[]'),'UTF8')),'hex') generation_state_sha256,
        encode(sha256(convert_to(coalesce((SELECT jsonb_agg(to_jsonb(s) ORDER BY id)::text
          FROM logical_source s),'[]'),'UTF8')),'hex') pointer_state_sha256""")[0]


def runtime_identity(pid, binary):
    if type(pid) is not int or pid <= 0:
        raise RuntimeError('an actual live runtime PID is required')
    image = Path('/proc') / str(pid) / 'exe'
    process = (Path('/proc') / str(pid) / 'stat').read_text()
    started = process.rsplit(')', 1)[1].split()[19]
    observed, expected = image.stat(), Path(binary).stat()
    if (observed.st_dev, observed.st_ino) != (expected.st_dev, expected.st_ino):
        raise RuntimeError('actual live executable differs from the supplied runtime binary')
    return (pid, started, observed.st_dev, observed.st_ino, observed.st_size,
            observed.st_mtime_ns, observed.st_ctime_ns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'advance', 'publish', 'rollback'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--socket', type=Path)
    parser.add_argument('--privileged', action='store_true')
    parser.add_argument('--runtime-binary', type=Path)
    parser.add_argument('--runtime-pid', type=int)
    parser.add_argument('--storage-root', type=Path)
    parser.add_argument('--acceptance', type=Path)
    parser.add_argument('--events', type=Path)
    parser.add_argument('--max-batches', type=int, default=1)
    args = parser.parse_args()
    value, manifest_sha = private_read(args.manifest)
    pins = validate_manifest(value)
    if value['operator_sha256'] != operator_sha():
        raise RuntimeError('manifest operator/package code changed')
    if not 1 <= args.max_batches <= 1000000:
        raise RuntimeError('bounded runner step count required')
    database = Database(args.database, args.user_id, socket=args.socket, privileged=args.privileged)
    lock_descriptor = None
    kernel = None
    try:
        if args.action != 'status':
            lock_file = args.manifest.with_name(args.manifest.name + '.operator.lock')
            lock_descriptor = os.open(lock_file, os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
            lock_meta = os.fstat(lock_descriptor)
            if not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_mode & 0o077 \
                    or lock_meta.st_uid not in {os.geteuid(), 0, int(os.environ.get('SUDO_UID', '-1'))}:
                raise RuntimeError('operator lock must remain private and manifest-owner bound')
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.runtime_binary is None or args.runtime_pid is None \
                    or args.storage_root is None or args.events is None:
                raise RuntimeError('mutations require actual runtime/storage identities and durable events')
            if file_sha(args.runtime_binary) != value['runtime_binary_sha256']:
                raise RuntimeError('actual runtime binary differs')
            binary_identity = runtime_identity(args.runtime_pid, args.runtime_binary)
            release = load_module('posting_capacity_release', HERE / 'release-candidate.py')
            capacity = load_module('posting_capacity_kernel', HERE / 'legacy_capacity.py')
            full_check = lambda: release.thin_pool_capacity(args.storage_root,
                value['maximum_batch_growth_bytes'], require_estimate=True)
            initial = full_check()
            if initial is not None:
                kernel = capacity.LiveKernelCapacity(full_check, args.storage_root)
            directory = database.query("SELECT current_setting('data_directory') AS directory")[0]['directory']
            if Path(directory).stat().st_dev != args.storage_root.stat().st_dev:
                raise RuntimeError('database and admitted storage root are on different devices')
        def identity_gate(_manifest, _pin):
            if state_identities(database) != {k: value[k] for k in ('generation_state_sha256', 'pointer_state_sha256')}:
                raise RuntimeError('retained generation or pointer state changed')
            if runtime_identity(args.runtime_pid, args.runtime_binary) != binary_identity:
                raise RuntimeError('live runtime PID or executable changed during conversion')
            contract = database.query("SELECT encode(storage_v2_posting_conversion_contract_sha256(),'hex') sha256")[0]['sha256']
            if contract != value['catalog_contract_sha256']:
                raise RuntimeError('actual reader/trigger/view contract changed')
        def resource_gate(_manifest, _pin, _bytes):
            free = os.statvfs(args.storage_root)
            if free.f_bavail * free.f_frsize < value['minimum_free_bytes'] + value['maximum_batch_growth_bytes']:
                raise RuntimeError('actual filesystem reserve is insufficient')
            if kernel is not None:
                kernel.observe()
            wal = database.query('SELECT coalesce(sum(size),0) AS bytes FROM pg_ls_waldir()')[0]['bytes']
            if wal >= WAL_STOP_BYTES:
                raise RuntimeError('actual WAL exceeds the writer stop boundary')
        def acceptance_gate(_manifest, pin, state):
            if args.acceptance is None:
                raise RuntimeError('exact correctness acceptance is required before publication')
            accepted, _ = private_read(args.acceptance)
            binding = dict(manifest_sha256=manifest_sha, operator_sha256=value['operator_sha256'],
                           runtime_binary_sha256=value['runtime_binary_sha256'],
                           catalog_contract_sha256=value['catalog_contract_sha256'])
            documents = accepted.get('documents', [accepted])
            matching = [d for d in documents if d.get('document_id') == pin.document_id]
            exact = matching[0] if len(matching) == 1 else {}
            if accepted.get('schema_version') != 'mainrag.storage-v2.posting-compaction-acceptance.v1' \
                    or accepted.get('bindings') != binding or accepted.get('status') != 'PASS' \
                    or accepted.get('gate') != 'EXACT_REPRESENTATION_AND_READER_CORRECTNESS' \
                    or exact.get('posting_count') != state['posting_count'] \
                    or exact.get('pairs_sha256') != state['pairs_sha256'] \
                    or type(accepted.get('observed_at_unix')) is not int \
                    or not 0 <= accepted['observed_at_unix'] <= time.time() \
                    or not isinstance(accepted.get('proofs'), list) or not accepted['proofs']:
                raise RuntimeError('correctness acceptance is bound to a different representation/contract')
            for proof in accepted['proofs']:
                if set(proof) != {'file', 'sha256'} or Path(proof['file']).name != proof['file']:
                    raise RuntimeError('acceptance proof reference is invalid')
                _, actual = private_read(args.acceptance.parent / proof['file'])
                if actual != proof['sha256']:
                    raise RuntimeError('underlying correctness proof differs')
        def event_sink(event):
            parent = args.events.parent.resolve(strict=True).stat()
            owners = {os.geteuid(), 0, int(os.environ.get('SUDO_UID', '-1'))}
            if parent.st_mode & 0o077 or parent.st_uid not in owners:
                raise RuntimeError('event directory must remain private and manifest-owner bound')
            descriptor = os.open(args.events, os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid not in owners:
                    raise RuntimeError('event sink must remain a private regular file')
                raw = (json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n').encode()
                with os.fdopen(descriptor, 'ab', closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
        operator = PostingConversion(database, manifest_sha, identity_gate=identity_gate,
            resource_gate=resource_gate, acceptance_gate=acceptance_gate, durable_event=event_sink,
            max_batch_bytes=value['max_batch_bytes'])
        remaining = args.max_batches
        for pin in (pins if args.action != 'status' else []):
            state = operator.status(pin)
            if args.action == 'advance':
                while remaining and (state is None or not state['complete']):
                    state = operator.advance(pin)
                    remaining -= 1
                if remaining == 0:
                    break
            elif args.action == 'publish':
                state = operator.publish(pin)
            elif args.action == 'rollback':
                state = operator.restore_flat_visibility(pin)
        states = operator.status_many(pins)
        print(json.dumps(dict(manifest_sha256=manifest_sha, prepared=sum(s is not None for s in states),
            complete=sum(bool(s and s['complete']) for s in states),
            published=sum(bool(s and s['phase'] == 'COMPACT') for s in states),
            committed_postings=sum(s['posting_count'] for s in states if s), physical_reclaim_bytes=None)))
    finally:
        if kernel is not None:
            kernel.observer.close()
        if lock_descriptor is not None:
            os.close(lock_descriptor)


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        print('Reconcile committed receipts before continuing.', file=sys.stderr)
        raise SystemExit(1)
    except Exception:
        print('Posting compaction stopped; reconcile committed receipts before continuing.', file=sys.stderr)
        raise SystemExit(1)
