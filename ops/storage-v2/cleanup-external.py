#!/usr/bin/env python3
"""Exact Qdrant/component retirement with durable dispatch and read-only reconciliation."""
from __future__ import annotations

import argparse
import copy
import fcntl
import importlib.util
import json
import os
import re
import select
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


A = module('external_postgres_cleanup', 'cleanup-apply.py')
P = module('external_components', 'cleanup-components.py')
C, M = A.C, A.M
KINDS = frozenset({'qdrant_alias', 'qdrant_collection', 'systemd_unit', 'docker_container'})


def operator_digest():
    return A.digest({name: A.hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                     for name in ('cleanup-external.py', 'cleanup-components.py', 'cleanup-apply.py',
                                  'cleanup-plan.py', 'cleanup-manifest.py')})


def qdrant_mutation(origin, key, method, path, body=None):
    origin = C.qdrant_origin(origin)
    headers = {'Content-Type': 'application/json'}
    if key is not None:
        headers['api-key'] = key
    request = urllib.request.Request(origin+path, method=method, headers=headers,
        data=None if body is None else C.canonical(body))
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), C.NoRedirect).open(request, timeout=60) as response:
            raw = response.read(2*1024*1024+1)
        value = json.loads(raw, object_pairs_hook=C.unique_keys)
    except (OSError, ValueError, urllib.error.HTTPError) as error:
        raise RuntimeError('Qdrant outcome is unknown; read-only reconciliation is required') from error
    if len(raw) > 2*1024*1024 or not isinstance(value, dict) \
            or value.get('status') != 'ok' or value.get('result') is not True:
        raise RuntimeError('Qdrant did not confirm the operation; reconcile its outcome')


def vector_space(root, privileged=False):
    if privileged:
        script = """import importlib.util,json,sys
s=importlib.util.spec_from_file_location('vector_measurement',sys.argv[1])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
print(json.dumps(m.vector_space(sys.argv[2])))
"""
        return json.loads(P.command([sys.executable, '-c', script, str(Path(__file__).resolve()), str(root)], True).stdout)
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('vector storage root must be an existing nonsymlink directory')
    metadata = root.stat()
    allocated = metadata.st_blocks*512
    seen = {(metadata.st_dev, metadata.st_ino)}
    for current, directories, files in os.walk(root, followlinks=False):
        for name in directories+files:
            item = Path(current)/name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_dev != metadata.st_dev \
                    or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise RuntimeError('vector storage contains a symlink, foreign mount or special object')
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                allocated += info.st_blocks*512
                seen.add(identity)
            if len(seen) > 1000000:
                raise RuntimeError('vector storage scan exceeds its entry bound')
    usage = os.statvfs(root)
    return {'root': str(root.resolve()), 'device': metadata.st_dev, 'inode': metadata.st_ino,
            'allocated_bytes': allocated, 'filesystem_available_bytes': usage.f_bavail*usage.f_frsize}


def external_inventory(origin, key_file, specs, privileged=False):
    rows = [P.observe(spec, privileged) for spec in P.validate_specs(specs)]
    return {'schema_version': 'mainrag.storage-v2.cleanup-external-inventory.v1',
            'qdrant': C.qdrant_inventory(origin, key_file), 'components': rows,
            'container_ids': P.container_ids(privileged) if any(r['kind'] == 'docker_container' for r in rows) else None}


def build_plan(inventory, catalog_sha, draft, draft_sha, runtime_sha, legacy_sha, storage, origin):
    origin = C.qdrant_origin(origin)
    external = {'schema_version': 'mainrag.storage-v2.cleanup-external-inventory.v1',
                'qdrant': inventory.get('qdrant'), 'components': inventory.get('components'),
                'container_ids': inventory.get('container_ids')}
    if not A.HEX.fullmatch(runtime_sha) or not A.HEX.fullmatch(legacy_sha):
        raise RuntimeError('runtime and committed PostgreSQL phase identities are required')
    decisions = {item['key']: {k: item[k] for k in ('key', 'disposition', 'reason', 'authority')}
                 for item in draft.get('objects', []) if item.get('disposition') in ('KEEP', 'DELETE')}
    if M.draft(inventory, catalog_sha, decisions) != draft:
        raise RuntimeError('external dispositions differ from the exact protected inventory')
    if not isinstance(external['qdrant'], dict) or not isinstance(external['components'], list):
        raise RuntimeError('Qdrant and exact operational components must be inventoried')
    P.validate_specs({'schema_version': 'mainrag.storage-v2.cleanup-components-config.v1',
                      'components': [{k: r[k] for k in ('kind', 'identity', 'role')}
                                     for r in external['components']]})
    containers = [r for r in external['components'] if r['kind'] == 'docker_container']
    ids = external['container_ids']
    if containers and (not isinstance(ids, list) or len(ids) > 10000 or ids != sorted(set(ids))
            or any(not isinstance(value, str) or not P.CONTAINER.fullmatch(value) for value in ids)
            or any(r['identity'] not in ids and r.get('absent') is not True for r in containers)) \
            or not containers and ids is not None:
        raise RuntimeError('complete Docker identity inventory is missing or invalid')
    if external['qdrant'].get('schema_version') != 'mainrag.storage-v2.cleanup-qdrant.v1':
        raise RuntimeError('exact Qdrant inventory is required')
    if not isinstance(storage, dict) or set(storage) != {'root', 'device', 'inode', 'allocated_bytes',
                                                       'filesystem_available_bytes'} \
            or not isinstance(storage['root'], str) or not Path(storage['root']).is_absolute() \
            or any(type(storage[k]) is not int or storage[k] < 0 for k in storage if k != 'root'):
        raise RuntimeError('exact vector storage identity and allocation are required')
    if any(item['disposition'] == 'UNREVIEWED' for item in draft['objects']):
        raise RuntimeError('external cleanup requires complete dispositions')
    targets = [r for r in draft['objects'] if r['kind'] in KINDS and r['disposition'] == 'DELETE']
    if not targets:
        raise RuntimeError('no reviewed external targets')
    deleted_aliases = {r['observed']['alias_name'] for r in targets if r['kind'] == 'qdrant_alias'}
    deleted_collections = {r['observed']['name'] for r in targets if r['kind'] == 'qdrant_collection'}
    for row in external['qdrant']['aliases']:
        if row['collection_name'] in deleted_collections and row['alias_name'] not in deleted_aliases:
            raise RuntimeError('retained alias references a deleted collection')
    unit_targets = {r['observed']['identity'] for r in targets if r['kind'] == 'systemd_unit'}
    component_specs = {r['identity']: r for r in external['components'] if r['kind'] == 'systemd_unit'}
    for row in targets:
        observed = row['observed']
        if row['kind'] in ('systemd_unit', 'docker_container'):
            if observed['role'] == 'retained' or P.retired(observed):
                raise RuntimeError('retained or already retired component cannot be a new delete target')
            if row['kind'] == 'systemd_unit':
                if observed['properties']['LoadState'] != 'loaded' or not observed['definitions']:
                    raise RuntimeError('component definition is not available')
                if observed['pending_job']:
                    raise RuntimeError('component transition is still live')
                for trigger in observed['properties']['TriggeredBy'].split():
                    if trigger not in unit_targets or component_specs[trigger]['role'] != 'producer':
                        raise RuntimeError('every component activator must be explicitly retired first')
                if observed['role'] == 'producer' and any(name not in unit_targets
                        for name in observed['properties']['Triggers'].split()):
                    raise RuntimeError('a producer still activates an unreviewed or retained component')
            if observed['role'] == 'qdrant_backend' and deleted_collections != {
                    r['name'] for r in external['qdrant']['collections']}:
                raise RuntimeError('Qdrant backend still serves retained collections')
    if sum(r['kind'] in ('systemd_unit', 'docker_container') and r['observed']['role'] == 'qdrant_backend'
           for r in targets) > 1:
        raise RuntimeError('Qdrant endpoint must have one exact backend retirement owner')
    order = lambda r: (0 if r['kind'] in ('systemd_unit', 'docker_container') and r['observed']['role'] == 'producer'
                      else 1 if r['kind'] == 'qdrant_alias' else 2 if r['kind'] == 'qdrant_collection'
                      else 3, 0 if r['kind'] == 'systemd_unit' and r['observed']['identity'].endswith(('.timer', '.socket')) else 1,
                      r['key'])
    return {'schema_version': 'mainrag.storage-v2.external-cleanup-plan.v1',
            'phase': 'QDRANT_AND_COMPONENTS', 'status': 'PLANNED_REQUIRES_LIVE_GATES',
            'catalog_file_sha256': catalog_sha, 'draft_file_sha256': draft_sha,
            'catalog': inventory['catalog'], 'qdrant_origin': origin,
            'before_state_sha256': A.digest({'catalog': inventory['catalog'], 'external': external}),
            'pointer_set_sha256': inventory['catalog']['pointer_set_sha256'],
            'runtime_package_sha256': runtime_sha, 'legacy_cleanup_manifest_sha256': legacy_sha,
            'external_before': external, 'objects': draft['objects'], 'targets': sorted(targets, key=order),
            'operator_sha256': operator_digest(), 'vector_space_before': storage,
            'remaining_phases': ['NATIVE_GC_REPACK', 'POST_CLEANUP_ACCEPTANCE']}


def validate_gates(plan, plan_sha, approval, evidence_root, now, privileged=False):
    gates = A.validate_approval(plan, plan_sha, approval, evidence_root, now)
    A.validate_runtime(plan, plan['legacy_cleanup_manifest_sha256'], gates, privileged)
    reviews = gates['dependency_and_caller_review'].get('external_consumers')
    if not isinstance(reviews, list) or len(reviews) != len(plan['targets']):
        raise RuntimeError('every external target needs an exact retired-consumer proof')
    expected = {r['key']: r['observed_sha256'] for r in plan['targets']}
    seen = set()
    for row in reviews:
        if not isinstance(row, dict) or row.get('key') in seen \
                or expected.get(row.get('key')) != row.get('observed_sha256') \
                or row.get('legacy_exclusive') is not True or row.get('other_consumers_absent') is not True:
            raise RuntimeError('external consumer coverage or identity differs')
        seen.add(row['key'])
    storage = gates['runtime_retirement'].get('vector_storage')
    if storage != {k: plan['vector_space_before'][k] for k in ('root', 'device', 'inode')} \
            or gates['runtime_retirement'].get('exclusive_qdrant_storage') is not True:
        raise RuntimeError('vector storage ownership and exact directory identity are unproven')
    return gates


def expected_after(before, target):
    result = copy.deepcopy(before)
    observed = target['observed']
    if target['kind'] == 'qdrant_alias':
        result['qdrant']['aliases'] = [r for r in result['qdrant']['aliases'] if r['alias_name'] != observed['alias_name']]
    elif target['kind'] == 'qdrant_collection':
        result['qdrant']['collections'] = [r for r in result['qdrant']['collections'] if r['name'] != observed['name']]
    else:
        if target['kind'] == 'docker_container':
            result['container_ids'].remove(observed['identity'])
        for row in result['components']:
            if (row['kind'], row['identity']) == (observed['kind'], observed['identity']):
                row.clear()
                row.update(kind=observed['kind'], identity=observed['identity'], role=observed['role'], retired=True)
    return normalized(result)


def normalized(value):
    value = copy.deepcopy(value)
    retired_units = {row['identity'] for row in value['components']
                     if row['kind'] == 'systemd_unit' and P.retired(row)}
    for row in value['components']:
        if P.retired(row):
            identity = {k: row[k] for k in ('kind', 'identity', 'role')}
            row.clear()
            row.update(**identity, retired=True)
        elif row['kind'] == 'systemd_unit':
            # Masking an approved activator can remove its logical references
            # during daemon-reload. Both manager representations denote the
            # same admitted retirement; no unrelated property is ignored.
            for key in ('TriggeredBy', 'Triggers'):
                row['properties'][key] = ' '.join(name for name in row['properties'][key].split()
                                                  if name not in retired_units)
    return value


def dispatch(target, origin, key_file, manifest_sha, privileged=False):
    row = target['observed']
    if target['kind'] == 'qdrant_alias':
        qdrant_mutation(origin, C.qdrant_key(key_file), 'POST', '/collections/aliases?timeout=45',
                        {'actions': [{'delete_alias': {'alias_name': row['alias_name']}}]})
    elif target['kind'] == 'qdrant_collection':
        qdrant_mutation(origin, C.qdrant_key(key_file), 'DELETE',
                        '/collections/'+urllib.parse.quote(row['name'], safe='')+'?timeout=45')
    else:
        P.retire({k: row[k] for k in ('kind', 'identity', 'role')}, row, manifest_sha, privileged)


def journal_event(directory, index, value, previous=None):
    return C.private_create(directory/f'{index:05d}.json',
        {'sequence': index, 'previous_sha256': previous, 'event': value})


def apply_steps(plan, plan_sha, attempt, observe, guard, mutate):
    # The dispatcher calls guard before every mutation and checks the complete
    # external set afterward. No changed retained object is silently accepted.
    expected = normalized(plan['external_before'])
    guard()
    if normalized(observe()) != expected:
        raise RuntimeError('external before-state drifted')
    attempt.mkdir(mode=0o700)
    directory = os.open(attempt.parent, os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    previous = journal_event(attempt, 0, {'status': 'ADMITTED', 'manifest_sha256': plan_sha,
                             'expected': expected, 'operator_sha256': operator_digest()})
    for number, target in enumerate(plan['targets'], 1):
        guard()
        observed = observe()
        if normalized(observed) != expected:
            raise RuntimeError('external state drifted before dispatch')
        after = expected_after(expected, target)
        previous = journal_event(attempt, number*2-1, {'status': 'DISPATCHED_OUTCOME_UNCONFIRMED',
                      'target_key': target['key'], 'before': expected, 'expected_after': after}, previous)
        guard()
        effective = copy.deepcopy(target)
        if target['kind'] in ('systemd_unit', 'docker_container'):
            effective['observed'] = next(row for row in observed['components']
                if (row['kind'], row['identity']) == (target['kind'], target['observed']['identity']))
        mutate(effective)
        current = normalized(observe())
        if current != after:
            raise RuntimeError('external outcome or retained state differs; reconcile without retry')
        guard()
        previous = journal_event(attempt, number*2, {'status': 'CONFIRMED_BY_LIVE_READBACK', 'after': current}, previous)
        expected = current
    result = {'status': 'EXTERNAL_TARGETS_RETIRED', 'manifest_sha256': plan_sha,
              'after_state_sha256': A.digest(expected), 'target_count': len(plan['targets']),
              'journal_tail_sha256': previous,
              'physical_reclamation_verified': False, 'final_cleanup_acceptance': False,
              'remaining_phases': plan['remaining_phases']}
    C.private_create(attempt/'completed.json', result)
    return result


def read_journal(plan, plan_sha, attempt):
    if attempt.is_symlink() or stat.S_IMODE(attempt.stat().st_mode) & 0o077:
        raise RuntimeError('attempt directory is not private')
    entries = sorted(attempt.glob('[0-9][0-9][0-9][0-9][0-9].json'))
    if not entries or len(entries) > len(plan['targets'])*2+1:
        raise RuntimeError('attempt journal is empty or excessive')
    expected = normalized(plan['external_before'])
    previous = None
    after = None
    last = None
    for index, path in enumerate(entries):
        row, sha = M.private_read(path, 128*1024*1024)
        if path.name != f'{index:05d}.json' or set(row) != {'sequence', 'previous_sha256', 'event'} \
                or row['sequence'] != index or row['previous_sha256'] != previous:
            raise RuntimeError('attempt journal chain or sequence differs')
        event = row['event']
        if index == 0:
            required = {'status': 'ADMITTED', 'manifest_sha256': plan_sha,
                        'expected': expected, 'operator_sha256': operator_digest()}
        elif index % 2:
            target = plan['targets'][(index-1)//2]
            after = expected_after(expected, target)
            required = {'status': 'DISPATCHED_OUTCOME_UNCONFIRMED', 'target_key': target['key'],
                        'before': expected, 'expected_after': after}
        else:
            required = {'status': 'CONFIRMED_BY_LIVE_READBACK', 'after': after}
            expected = after
        if event != required:
            raise RuntimeError('attempt journal does not match the exact manifest prefix')
        previous, last = sha, event
    completed = attempt/'completed.json'
    if completed.exists() or completed.is_symlink():
        result, _ = M.private_read(completed, 1024*1024)
        if len(entries) != len(plan['targets'])*2+1 or result.get('journal_tail_sha256') != previous \
                or result.get('manifest_sha256') != plan_sha \
                or result.get('after_state_sha256') != A.digest(expected):
            raise RuntimeError('completed receipt and journal prefix differ')
    return last


def reconcile(plan, plan_sha, attempt, observe):
    last = read_journal(plan, plan_sha, attempt)
    current = normalized(observe())
    expected = last.get('after', last.get('expected_after', last.get('expected')))
    matched = current == expected
    return {'status': 'READBACK_MATCHES_LAST_STEP' if matched else 'OUTCOME_UNRESOLVED_OR_DRIFTED',
            'manifest_sha256': plan_sha, 'last_event_status': last['status'],
            'last_step_confirmed': matched, 'mutation_performed': False, 'automatic_retry_allowed': False,
            'current_state_sha256': A.digest(current), 'final_cleanup_acceptance': False}


class DatabaseLease:
    """One live connection serializes external retirement, SQL cleanup and GC.

    SHARE locks keep native catalog/data writes from changing the admitted
    state. Queries use other short read-only connections; the lease is never
    represented by a stale file or a saved PID.
    """
    def __init__(self, database, privileged, catalog):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', database):
            raise RuntimeError('database must be a local database name')
        self.database, self.privileged, self.catalog = database, privileged, catalog
        self.process = None
        self.backend_pid = None

    def query(self, statement):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError('external cleanup database lease is not live')
        self.process.stdin.write(statement+'\n')
        self.process.stdin.flush()
        # Read one bounded line without relying on TextIO buffering. The
        # statements below emit exactly one marker and psql runs in quiet mode.
        output = b''
        deadline = time.monotonic()+15
        while not output.endswith(b'\n'):
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise RuntimeError('database lease observation timed out; no dispatch is permitted')
            byte = os.read(self.process.stdout.fileno(), 1)
            if not byte or len(output) > 1024:
                raise RuntimeError('database lease observation failed')
            output += byte
        return output.decode().strip()

    def __enter__(self):
        args = (['sudo', '-n', '-u', 'postgres', 'env', 'PGAPPNAME=mainrag-storage-v2-external-cleanup']
                if self.privileged else [])+[
            'psql', '-X', '--no-psqlrc', '-qAt', '--set=ON_ERROR_STOP=1', '--dbname', self.database]
        env = os.environ.copy()
        env['PGAPPNAME'] = 'mainrag-storage-v2-external-cleanup'
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, env=env)
        try:
            for key in ('mainrag.external-cleanup', 'mainrag.legacy-cleanup', 'mainrag.native-gc'):
                result = self.query("SELECT CASE WHEN pg_try_advisory_lock(hashtextextended("+
                                    A.literal(key)+",0)) THEN 'LOCKED' ELSE 'BUSY' END;")
                if result != 'LOCKED':
                    raise RuntimeError('a cleanup/GC lease is live; observe it instead of dispatching')
            locks = '\n'.join('LOCK TABLE ONLY public.'+A.ident(row['name'])+' IN SHARE MODE;'
                for row in sorted(self.catalog['relations'], key=lambda r: A.oid(r['oid']))
                if row.get('kind') in ('r', 'p', 'm'))
            if self.query("BEGIN; SET LOCAL lock_timeout='2s'; SET LOCAL idle_in_transaction_session_timeout='600s';\n"+
                          locks+"\nSELECT 'ADMITTED';") != 'ADMITTED':
                raise RuntimeError('native write exclusion could not be proven')
            self.backend_pid = int(self.query('SELECT pg_backend_pid();'))
            if self.backend_pid <= 1:
                raise RuntimeError('database lease backend identity is invalid')
            return self
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.backend_pid is None or self.query('SELECT pg_backend_pid();') != str(self.backend_pid):
            raise RuntimeError('database cleanup lease is no longer live')

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                try:
                    self.process.stdin.write('ROLLBACK;\n\\q\n')
                    self.process.stdin.flush()
                    self.process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.terminate()
                    self.process.wait(timeout=5)
            self.process.stdin.close()
            self.process.stdout.close()

    def __exit__(self, *unused):
        self.close()


class LiveObserver:
    def __init__(self, plan, key_file, privileged, prior=None):
        self.plan, self.key_file, self.privileged = plan, key_file, privileged
        self.last_qdrant = prior['qdrant'] if prior is not None else None
        self.backend_stop_proven = False

    def __call__(self):
        rows = [P.observe({k: row[k] for k in ('kind', 'identity', 'role')}, self.privileged)
                for row in self.plan['external_before']['components']]
        stopped = any(row['role'] == 'qdrant_backend' and P.retired(row) for row in rows)
        if stopped:
            # An HTTP failure alone cannot prove deletion. First there must be
            # a confirmed empty API readback, then an exact stopped backend and
            # an explicit TCP refusal. Do not invent a fresh HTTP inventory.
            if self.last_qdrant is None or self.last_qdrant['collections'] or self.last_qdrant['aliases']:
                raise RuntimeError('backend stopped without a confirmed empty Qdrant readback')
            endpoint = urllib.parse.urlparse(self.plan['qdrant_origin'])
            try:
                connection = socket.create_connection((endpoint.hostname, endpoint.port), timeout=5)
            except ConnectionRefusedError:
                self.backend_stop_proven = True
            except OSError as error:
                raise RuntimeError('backend endpoint failure is not proof of retirement') from error
            else:
                connection.close()
                raise RuntimeError('retired Qdrant backend still has a listening endpoint')
            # Qdrant's collection storage subtree must be empty; no path here
            # is removed by this operator. Retained database/pack roots are not
            # part of vector-space measurement or deletion.
            script = """import os,stat,sys
p=os.path.join(sys.argv[1],'collections')
if os.path.lexists(p):
 if not stat.S_ISDIR(os.lstat(p).st_mode) or os.listdir(p):raise RuntimeError('remaining collection storage')
"""
            check = P.command([sys.executable, '-c', script, self.plan['vector_space_before']['root']],
                              self.privileged, missing=True)
            if check.returncode:
                raise RuntimeError('retired backend still has collection storage entries')
            qdrant = self.last_qdrant
        else:
            qdrant = C.qdrant_inventory(self.plan['qdrant_origin'], self.key_file)
            self.last_qdrant = qdrant
        return {'schema_version': 'mainrag.storage-v2.cleanup-external-inventory.v1',
                'qdrant': qdrant, 'components': rows,
                'container_ids': P.container_ids(self.privileged)
                    if any(r['kind'] == 'docker_container' for r in rows) else None}


def native_guard(plan, gates, database, privileged, lease):
    lease.check()
    A.validate_runtime(plan, plan['legacy_cleanup_manifest_sha256'], gates, privileged)
    parent = A.receipt(database, privileged, plan['legacy_cleanup_manifest_sha256'])
    if not isinstance(parent, dict) or parent.get('pointer_set_sha256') != plan['pointer_set_sha256'] \
            or parent.get('runtime_package_sha256') != plan['runtime_package_sha256'] \
            or parent.get('result', {}).get('status') != 'DB_COMMITTED_POSTCHECK_PENDING':
        raise RuntimeError('committed PostgreSQL phase and live runtime bindings differ')
    catalog = plan['catalog']
    reach = catalog['reachability']
    current = C.catalog(database, privileged, tuple(sorted(catalog['exact_rows'])),
                         tuple(reach['retained_generation_ids']) if reach else ())
    if current != catalog or current['open_reader_count'] or current['building_run_count'] \
            or not current['active_pointer_count']:
        changed = sorted(key for key in set(current)|set(catalog) if current.get(key) != catalog.get(key))
        raise RuntimeError('native catalog, pointers, readers or writers drifted; changed fields: '+','.join(changed))
    active = A.psql(database, privileged, "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
        "WHERE datid=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "AND pid<>pg_backend_pid() AND backend_type='client backend' AND state<>'idle' "
        f"AND pid<>{int(lease.backend_pid)});")
    if active != 'f':
        raise RuntimeError('another database reader/writer appeared during external retirement')
    script = """import json,os,stat,sys
from pathlib import Path
p=Path(sys.argv[1]);s=p.lstat()
if not stat.S_ISDIR(s.st_mode):raise RuntimeError('root is not a directory')
print(json.dumps({'root':str(p.resolve()),'device':s.st_dev,'inode':s.st_ino}))
"""
    usage = json.loads(P.command([sys.executable, '-c', script, plan['vector_space_before']['root']], privileged).stdout)
    if any(usage[k] != plan['vector_space_before'][k] for k in ('root', 'device', 'inode')):
        raise RuntimeError('vector storage root identity drifted')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--apply')
    modes.add_argument('--reconcile')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--catalog', required=True, type=Path)
    parser.add_argument('--catalog-sha256')
    parser.add_argument('--draft', required=True, type=Path)
    parser.add_argument('--draft-sha256')
    parser.add_argument('--runtime-package-sha256')
    parser.add_argument('--legacy-cleanup-manifest-sha256')
    parser.add_argument('--qdrant-url')
    parser.add_argument('--qdrant-api-key-file', type=Path)
    parser.add_argument('--vector-storage-root', type=Path)
    parser.add_argument('--approval', type=Path)
    parser.add_argument('--approval-sha256')
    parser.add_argument('--database')
    parser.add_argument('--local-postgres', action='store_true')
    parser.add_argument('--attempt', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.plan:
        if None in (args.runtime_package_sha256, args.legacy_cleanup_manifest_sha256,
                    args.qdrant_url, args.vector_storage_root):
            parser.error('planning requires exact runtime, committed SQL phase, endpoint and storage root')
        inventory = A.read_bound(args.catalog, args.catalog_sha256)
        draft = A.read_bound(args.draft, args.draft_sha256)
        plan = build_plan(inventory, args.catalog_sha256, draft, args.draft_sha256,
                          args.runtime_package_sha256, args.legacy_cleanup_manifest_sha256,
                          vector_space(args.vector_storage_root, args.local_postgres), args.qdrant_url)
        sha = C.private_create(args.manifest, plan)
        print(json.dumps({'status': plan['status'], 'sha256': sha, 'target_count': len(plan['targets'])}))
        return 0
    if None in (args.database, args.approval, args.approval_sha256, args.attempt) \
            or (args.reconcile and args.output is None):
        parser.error('apply/reconcile requires exact authority, local database and protected attempt; reconcile also needs output')
    plan_sha = args.apply or args.reconcile
    plan = A.read_bound(args.manifest, plan_sha)
    if plan.get('schema_version') != 'mainrag.storage-v2.external-cleanup-plan.v1' \
            or plan.get('operator_sha256') != operator_digest():
        raise RuntimeError('external cleanup manifest or operator identity differs')
    inventory = A.read_bound(args.catalog, plan['catalog_file_sha256'])
    draft = A.read_bound(args.draft, plan['draft_file_sha256'])
    if build_plan(inventory, plan['catalog_file_sha256'], draft, plan['draft_file_sha256'],
                  plan['runtime_package_sha256'], plan['legacy_cleanup_manifest_sha256'],
                  plan['vector_space_before'], plan['qdrant_origin']) != plan:
        raise RuntimeError('executable manifest differs from exact catalog and dispositions')
    approval = A.read_bound(args.approval, args.approval_sha256)
    # Reconciliation verifies historic authorization plus the *current* state;
    # it cannot dispatch, and never calls stale authorization fresh.
    reference_time = approval.get('observed_at_unix') if args.reconcile else int(time.time())
    gates = validate_gates(plan, plan_sha, approval, args.approval.parent, reference_time, args.local_postgres)
    prior = None
    if args.reconcile:
        last = read_journal(plan, plan_sha, args.attempt)
        prior = last.get('before', last.get('after', last.get('expected')))
    observer = LiveObserver(plan, args.qdrant_api_key_file, args.local_postgres, prior)
    descriptor = os.open(str(args.manifest)+'.lock', os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
        with DatabaseLease(args.database, args.local_postgres, plan['catalog']) as lease:
            guard = lambda: native_guard(plan, gates, args.database, args.local_postgres, lease)
            guard()
            for row in plan['external_before']['components']:
                if row['role'] != 'retained' and P.protects_process(row, gates['runtime_retirement']['runtime']['pid']):
                    raise RuntimeError('native API belongs to a proposed retirement target')
            if args.reconcile:
                result = reconcile(plan, plan_sha, args.attempt, observer)
                guard()
                C.private_create(args.output, result)
            else:
                current_space = vector_space(plan['vector_space_before']['root'], args.local_postgres)
                if any(current_space[k] != plan['vector_space_before'][k]
                       for k in ('root', 'device', 'inode', 'allocated_bytes')):
                    raise RuntimeError('vector storage allocation drifted since planning')
                result = apply_steps(plan, plan_sha, args.attempt, observer, guard,
                    lambda target: dispatch(target, plan['qdrant_origin'], args.qdrant_api_key_file, plan_sha, args.local_postgres))
            space = vector_space(plan['vector_space_before']['root'], args.local_postgres)
            C.private_create(args.attempt/('space-readback-'+str(time.time_ns())+'.json'), {
                'manifest_sha256': plan_sha, 'before': plan['vector_space_before'], 'after': space,
                'measured_vector_allocated_bytes_released': plan['vector_space_before']['allocated_bytes']-space['allocated_bytes'],
                'measured_filesystem_available_bytes_delta': space['filesystem_available_bytes']-plan['vector_space_before']['filesystem_available_bytes'],
                'backend_stop_proven': observer.backend_stop_proven,
                'qdrant_readback_method': 'PRIOR_CONFIRMED_EMPTY_API_PLUS_COMPONENT_AND_TCP_REFUSAL'
                    if observer.backend_stop_proven else 'LIVE_HTTP_INVENTORY',
                'filesystem_or_thinpool_reclaim_proven': False, 'final_cleanup_acceptance': False})
            print(json.dumps({'status': result['status'], 'manifest_sha256': plan_sha,
                              'final_cleanup_acceptance': False}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as error:
        # Raw external responses, filesystem paths and command stderr remain
        # private. A bounded operator failure is not permission to retry.
        print('External cleanup stopped: '+(str(error) if isinstance(error, RuntimeError) else
              'invalid protected input or unavailable live state')+'; reconcile before further mutation.', file=sys.stderr)
        raise SystemExit(2)
