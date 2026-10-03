#!/usr/bin/env python3
"""Observe and retire exact systemd units or Docker containers, without volumes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

UNIT = re.compile(r'[A-Za-z0-9_.@:-]+\.(service|socket|timer)\Z')
CONTAINER = re.compile(r'[0-9a-f]{64}\Z')
PROPERTIES = ('Id', 'LoadState', 'ActiveState', 'SubState', 'UnitFileState',
              'FragmentPath', 'DropInPaths', 'MainPID', 'ControlGroup',
              'ExecMainStartTimestampMonotonic', 'TriggeredBy', 'Triggers')


def command(arguments, privileged=False, *, missing=False):
    try:
        result = subprocess.run((['sudo', '-n'] if privileged else [])+arguments,
                                capture_output=True, text=True, timeout=60, check=False)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError('component outcome is unknown; reconcile without retry') from error
    if result.returncode and not missing:
        raise RuntimeError('component manager operation failed; inspect the exact target')
    if len(result.stdout) > 4*1024*1024:
        raise RuntimeError('component manager response exceeds its bound')
    return result


def validate_specs(value):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'components'} \
            or value['schema_version'] != 'mainrag.storage-v2.cleanup-components-config.v1' \
            or not isinstance(value['components'], list) or len(value['components']) > 128:
        raise RuntimeError('component configuration is incomplete')
    seen = set()
    for row in value['components']:
        if not isinstance(row, dict) or set(row) != {'kind', 'identity', 'role'} \
                or row['role'] not in ('producer', 'qdrant_backend', 'legacy_backend', 'retained'):
            raise RuntimeError('component role or identity is invalid')
        pattern = UNIT if row['kind'] == 'systemd_unit' else (
            CONTAINER if row['kind'] == 'docker_container' else None)
        if pattern is None or not isinstance(row['identity'], str) or not pattern.fullmatch(row['identity']):
            raise RuntimeError('only canonical unit names or complete container IDs are accepted')
        key = (row['kind'], row['identity'])
        if key in seen:
            raise RuntimeError('component identities are duplicated')
        seen.add(key)
    return value['components']


def container_ids(privileged=False):
    result = command(['docker', 'container', 'ls', '--all', '--no-trunc', '--format', '{{.ID}}'], privileged)
    rows = result.stdout.splitlines()
    if len(rows) > 10000 or any(not CONTAINER.fullmatch(row) for row in rows) or len(rows) != len(set(rows)):
        raise RuntimeError('complete Docker container identity set is invalid')
    return sorted(rows)


def file_identity(path, privileged=False):
    if privileged:
        script = """import importlib.util,json,sys
s=importlib.util.spec_from_file_location('component_identity',sys.argv[1])
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
print(json.dumps(m.file_identity(sys.argv[2])))
"""
        return json.loads(command([sys.executable, '-c', script, str(Path(__file__).resolve()), path], True).stdout)
    resolved = Path(path).resolve(strict=True)
    descriptor = os.open(resolved, os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 1024*1024:
            raise RuntimeError('component definition is not a bounded regular file')
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != \
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise RuntimeError('component definition changed while reading')
    return {'path': str(resolved), 'device': before.st_dev, 'inode': before.st_ino,
            'size': before.st_size, 'mtime_ns': before.st_mtime_ns, 'sha256': digest}


def observe(spec, privileged=False):
    validate_specs({'schema_version': 'mainrag.storage-v2.cleanup-components-config.v1', 'components': [spec]})
    identity = spec['identity']
    if spec['kind'] == 'systemd_unit':
        result = command(['systemctl', 'show', '--no-pager',
                          '--property='+','.join(PROPERTIES), '--', identity], privileged)
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if identity.endswith(('.timer', '.socket')):
            # These unit classes do not expose a service main-process identity.
            # Keep a reported cgroup; only its known non-service absence is
            # normalized. Other missing manager properties still fail closed.
            for key, default in (('MainPID', '0'), ('ControlGroup', ''),
                                 ('ExecMainStartTimestampMonotonic', '0')):
                values.setdefault(key, default)
        if values.get('Id') != identity or set(values) != set(PROPERTIES):
            raise RuntimeError('systemd returned another or incomplete unit identity')
        definitions = []
        if values['LoadState'] not in ('masked', 'not-found'):
            paths = ([values['FragmentPath']] if values['FragmentPath'] else [])+values['DropInPaths'].split()
            definitions = [file_identity(path, privileged) for path in paths]
        jobs = command(['systemctl', 'list-jobs', '--no-legend', '--no-pager', '--plain'], privileged).stdout
        pending = any(len(line.split()) >= 2 and line.split()[1] == identity for line in jobs.splitlines())
        return {**spec, 'properties': values, 'definitions': definitions, 'pending_job': pending}
    result = command(['docker', 'container', 'inspect', '--', identity], privileged, missing=True)
    if result.returncode:
        # A failed daemon/permission request is not proof of container absence.
        listed = command(['docker', 'container', 'ls', '--all', '--no-trunc', '--format', '{{.ID}}'], privileged)
        if identity in listed.stdout.splitlines():
            raise RuntimeError('existing container could not be inspected')
        return {**spec, 'absent': True}
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('Id') != identity:
        raise RuntimeError('Docker returned another container identity')
    row = rows[0]
    # Configuration may contain secrets: hash it in memory, never retain raw
    # inspect output or environment variables in the evidence artifact.
    digest = hashlib.sha256(json.dumps({k: row[k] for k in ('Config', 'HostConfig', 'Mounts')},
                                      sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    pids = []
    if row['State']['Running']:
        top = command(['docker', 'container', 'top', identity, '-eo', 'pid'], privileged)
        pids = sorted(int(v.strip()) for v in top.stdout.splitlines()[1:] if v.strip().isdigit())
    state = {k: row['State'][k] for k in ('Status', 'Running', 'Paused', 'Restarting',
                                         'OOMKilled', 'Dead', 'Pid', 'StartedAt', 'FinishedAt')}
    return {**spec, 'absent': False, 'image_id': row['Image'], 'state': state,
            'configuration_sha256': digest, 'process_ids': pids}


def retired(row):
    if row.get('retired') is True:
        return True
    if row['kind'] == 'docker_container':
        return row.get('absent') is True
    values = row['properties']
    return values['LoadState'] == 'masked' and values['UnitFileState'] in ('masked', 'masked-runtime') \
        and values['ActiveState'] in ('inactive', 'failed') and values['MainPID'] == '0' \
        and not row['pending_job']


def protects_process(row, pid):
    if row['kind'] == 'docker_container':
        return pid in row.get('process_ids', []) or row.get('state', {}).get('Pid') == pid
    group = row['properties']['ControlGroup']
    if row['properties']['MainPID'] == str(pid):
        return True
    if not group:
        return False
    groups = Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    return any(line.rsplit(':', 1)[-1] == group or
               line.rsplit(':', 1)[-1].startswith(group+'/') for line in groups)


def retire(spec, before, manifest_sha, privileged=False):
    if not isinstance(manifest_sha, str) or not CONTAINER.fullmatch(manifest_sha):
        raise RuntimeError('exact manifest identity is required')
    if observe(spec, privileged) != before:
        raise RuntimeError('component drifted before retirement')
    if before.get('pending_job') or spec['role'] == 'retained' or retired(before):
        raise RuntimeError('retained or transitioning component cannot be retired')
    if spec['kind'] == 'docker_container':
        if before.get('absent'):
            raise RuntimeError('container was absent before dispatch')
        # Complete ID only. Named and anonymous volumes are deliberately kept.
        command(['docker', 'container', 'rm', '--force', '--', spec['identity']], privileged)
    else:
        unit = spec['identity']
        if before['properties']['LoadState'] != 'loaded' or not before['definitions']:
            raise RuntimeError('component definition is absent or unavailable')
        command(['systemctl', 'disable', '--now', '--', unit], privileged)
        fragment = Path(before['properties']['FragmentPath'])
        # Custom units in the configuration directories cannot be masked over
        # an existing file. Retain the exact definition by same-directory rename;
        # never overwrite it or an existing retirement archive.
        if fragment.parent in (Path('/etc/systemd/system'), Path('/run/systemd/system')):
            expected = next((r for r in before['definitions'] if r['path'] == str(fragment)), None)
            if expected is None:
                raise RuntimeError('custom unit definition does not have an exact identity')
            script = """import hashlib,json,os,sys
p=sys.argv[1]; expected=json.loads(sys.argv[2]); dest=p+'.mainrag-retired-'+sys.argv[3]
fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
with os.fdopen(fd,'rb') as f:
 s=os.fstat(f.fileno()); h=hashlib.file_digest(f,'sha256').hexdigest()
 if (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,h)!=(expected['device'],expected['inode'],expected['size'],expected['mtime_ns'],expected['sha256']): raise RuntimeError('definition drift')
 current=os.lstat(p)
 if (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns): raise RuntimeError('definition path drift')
 os.link(p,dest,follow_symlinks=False)
 linked=os.lstat(dest)
 if (linked.st_dev,linked.st_ino)!=(s.st_dev,s.st_ino): raise RuntimeError('definition changed during preservation')
 os.unlink(p)
 directory=os.open(os.path.dirname(p),os.O_RDONLY|os.O_DIRECTORY);os.fsync(directory);os.close(directory)
"""
            command(['python3', '-c', script, str(fragment), json.dumps(expected), manifest_sha], privileged)
        command(['systemctl', 'daemon-reload'], privileged)
        command(['systemctl', 'mask', '--now', '--', unit], privileged)
    after = observe(spec, privileged)
    if not retired(after):
        raise RuntimeError('component retirement was not proven; reconcile before another action')
    return after
