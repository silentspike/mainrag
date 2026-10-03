"""Explicit owned external fixtures, separate from production cleanup gates."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

from eval.storage_v2.test_cleanup_external import E
from eval.storage_v2.test_cleanup_plan import catalog_fixture


def systemd_rehearsal():
    prefix = 'mainrag-external-fixture-'+uuid.uuid4().hex
    names = [prefix+'.service', prefix+'.timer']
    service = '[Unit]\nDescription=Owned external retirement fixture\n[Service]\nExecStart=/usr/bin/sleep 300\n[Install]\nWantedBy=multi-user.target\n'
    timer = '[Unit]\nDescription=Owned external retirement activator\n[Timer]\nOnActiveSec=600\nUnit='+names[0]+'\n[Install]\nWantedBy=timers.target\n'
    owned = []
    manifest_sha = '1'*64
    try:
        for name, definition in zip(names, (service, timer)):
            path = '/etc/systemd/system/'+name
            script = """import os,sys
fd=os.open(sys.argv[1],os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o644)
with os.fdopen(fd,'w') as f:f.write(sys.argv[2]);f.flush();os.fsync(f.fileno())
"""
            E.P.command([os.sys.executable, '-c', script, path, definition], True)
            owned.append(name)
        E.P.command(['systemctl', 'daemon-reload'], True)
        E.P.command(['systemctl', 'enable', '--now', '--', *names], True)
        specs = [{'kind': 'systemd_unit', 'identity': names[0], 'role': 'legacy_backend'},
                 {'kind': 'systemd_unit', 'identity': names[1], 'role': 'producer'}]
        observed = [E.P.observe(spec, True) for spec in specs]
        assert observed[0]['properties']['MainPID'] != '0'
        assert names[1] in observed[0]['properties']['TriggeredBy'].split()
        with tempfile.TemporaryDirectory(prefix='mainrag-external-journal-') as temporary:
            root = Path(temporary);root.chmod(0o700)
            before = {'schema_version': 'mainrag.storage-v2.cleanup-external-inventory.v1',
                      'qdrant': {'collections': [], 'aliases': []}, 'components': observed}
            targets = []
            for row in reversed(observed):
                targets.append({'kind': row['kind'], 'key': E.M.object_key(row['kind'], (row['identity'],)),
                                'observed': row})
            plan = {'external_before': before, 'targets': targets, 'remaining_phases': ['PRODUCTION_GATES']}
            def observe():
                return {**before, 'components': [E.P.observe(spec, True) for spec in specs]}
            result = E.apply_steps(plan, manifest_sha, root/'attempt', observe, lambda: None,
                                   lambda target: E.dispatch(target, None, None, manifest_sha, True))
            assert E.reconcile(plan, manifest_sha, root/'attempt', observe)['last_step_confirmed']
            assert all(E.P.retired(row) for row in observe()['components'])
            for row in observed:
                archived = '/etc/systemd/system/'+row['identity']+'.mainrag-retired-'+manifest_sha
                assert E.P.file_identity(archived, True)['sha256'] == row['definitions'][0]['sha256']
            print(json.dumps({'status': 'PASS_SYSTEMD_ACTIVATOR_AND_SERVICE_RETIRED',
                'owned_component_count': len(targets), 'definition_hashes_preserved': True,
                'production_cleanup_acceptance': False}))
    finally:
        # Complete fixture ownership was established by exclusive creation.
        # Never remove a path merely because it resembles the fixture prefix.
        for name in reversed(owned):
            E.P.command(['systemctl', 'disable', '--now', '--', name], True, missing=True)
            E.P.command(['systemctl', 'unmask', '--', name], True, missing=True)
            script = """import os,sys
for p in sys.argv[1:]:
 try:os.unlink(p)
 except FileNotFoundError:pass
"""
            path = '/etc/systemd/system/'+name
            E.P.command([os.sys.executable, '-c', script, path, path+'.mainrag-retired-'+manifest_sha], True)
        E.P.command(['systemctl', 'daemon-reload'], True)
        E.P.command(['systemctl', 'reset-failed', '--', *owned], True, missing=True)


def qdrant_rehearsal(origin):
    origin = E.C.qdrant_origin(origin)
    prefix = 'mainrag_external_fixture_'+uuid.uuid4().hex
    old, keep = prefix+'_old', prefix+'_keep'
    names, aliases = E.C.qdrant_names(origin, None)
    if {old, keep} & set(names):
        raise RuntimeError('fixture collection identity already exists')
    def request(method, path, body):
        call = urllib.request.Request(origin+path, data=E.C.canonical(body), method=method,
                                      headers={'Content-Type': 'application/json'})
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), E.C.NoRedirect).open(call, timeout=15) as response:
            value = json.loads(response.read(2*1024*1024))
        if value.get('status') != 'ok':
            raise RuntimeError('owned fixture setup request failed')
    try:
        for name in (old, keep):
            request('PUT', '/collections/'+name, {'vectors': {'size': 2, 'distance': 'Dot'}})
            request('PUT', '/collections/'+name+'/points?wait=true',
                    {'points': [{'id': 1, 'vector': [1.0, 0.0], 'payload': {'fixture': 'public retained point'}}]})
            request('POST', '/collections/aliases',
                    {'actions': [{'create_alias': {'alias_name': name+'_alias', 'collection_name': name}}]})
        retained = E.C.qdrant_response(origin, None, '/collections/'+keep+'/points/1')
        if retained.get('id') != 1 or retained.get('vector') != [1.0, 0.0] \
                or retained.get('payload') != {'fixture': 'public retained point'}:
            raise RuntimeError('retained fixture point/vector/payload was not actually observed')
        catalog = catalog_fixture()
        inventory = {'schema_version': 'mainrag.storage-v2.cleanup-catalog.v1', 'status': 'OBSERVED_ONLY',
                     'catalog': catalog, 'before_state_sha256': E.A.digest(catalog),
                     'operator_sha256': 'a'*64, 'components': [], 'qdrant': E.C.qdrant_inventory(origin, None)}
        decisions = {r['key']: {'key': r['key'], 'disposition': 'DELETE' if
                     r['observed'].get('name') == old or r['observed'].get('alias_name') == old+'_alias'
                     else 'KEEP', 'reason': 'owned public fixture only', 'authority': 'fixture'}
                     for r in E.M.observed_objects(inventory)}
        draft = E.M.draft(inventory, 'c'*64, decisions)
        with tempfile.TemporaryDirectory(prefix='mainrag-qdrant-rehearsal-') as temporary:
            root = Path(temporary);root.chmod(0o700)
            plan = E.build_plan(inventory, 'c'*64, draft, 'd'*64, 'e'*64, 'f'*64,
                                E.vector_space(root), origin)
            observer = E.LiveObserver(plan, None, False)
            def mutate(target):
                if target['observed'].get('name', target['observed'].get('alias_name')) not in (old, old+'_alias'):
                    raise RuntimeError('fixture dispatcher attempted an unowned target')
                E.dispatch(target, origin, None, '1'*64)
            E.apply_steps(plan, '1'*64, root/'attempt', observer, lambda: None, mutate)
            assert E.reconcile(plan, '1'*64, root/'attempt', observer)['last_step_confirmed']
            assert E.C.qdrant_response(origin, None, '/collections/'+keep+'/points/1') == retained
            current_names, current_aliases = E.C.qdrant_names(origin, None)
            assert old not in current_names and keep in current_names
            assert not any(r['alias_name'] == old+'_alias' for r in current_aliases)
            assert any(r['alias_name'] == keep+'_alias' for r in current_aliases)
            print(json.dumps({'status': 'PASS_REAL_QDRANT_EXACT_ALIAS_AND_COLLECTION_DELETE',
                'retained_point_and_vector_unchanged': True, 'retired_target_count': 2,
                'production_admission_tested': False, 'physical_reclamation_verified': False}))
    finally:
        current_names, current_aliases = E.C.qdrant_names(origin, None)
        for row in current_aliases:
            if row['alias_name'] in (old+'_alias', keep+'_alias'):
                E.qdrant_mutation(origin, None, 'POST', '/collections/aliases',
                                 {'actions': [{'delete_alias': {'alias_name': row['alias_name']}}]})
        for name in (old, keep):
            if name in current_names:
                E.qdrant_mutation(origin, None, 'DELETE', '/collections/'+name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--systemd', action='store_true')
    mode.add_argument('--qdrant-url')
    args = parser.parse_args()
    if args.systemd:
        systemd_rehearsal()
    else:
        qdrant_rehearsal(args.qdrant_url)


if __name__ == '__main__':
    main()
