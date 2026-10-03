"""Exact authority, retained targets and runtime identity for legacy retirement."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from eval.storage_v2.test_cleanup_manifest import fixture

SPEC=importlib.util.spec_from_file_location('cleanup_apply',Path(__file__).resolve().parents[2]/'ops/storage-v2/cleanup-apply.py')
A=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(A)


def prepared():
    inventory=fixture();c=inventory['catalog']
    c['relations'][0].update(kind='r')
    c['relations'].append({'oid':9,'name':'storage_v2_legacy_cleanup_receipt','kind':'r'})
    c['exact_rows']={'fixture':3};c['routine_relation_references']=[]
    inventory['before_state_sha256']=A.digest(c)
    objects=A.M.observed_objects(inventory)
    decisions={item['key']:{'key':item['key'],'disposition':'DELETE' if item['kind']=='relation' and item['observed']['name']=='fixture' else 'KEEP',
                           'reason':'public fixture only','authority':'fixture'} for item in objects}
    draft=A.M.draft(inventory,'c'*64,decisions)
    return inventory,draft,A.build_plan(inventory,'c'*64,draft,'d'*64,'e'*64)


class CleanupApplyTests(unittest.TestCase):
    def test_native_functions_and_children_cannot_be_deleted(self):
        for name in ('storage_v2_commit_shadow_ingest','storage_v2_search_active','storage_v2_put_content_body'):
            with self.subTest(name=name),self.assertRaisesRegex(RuntimeError,'cannot be deleted'):
                A.M.validate_delete({'kind':'function','disposition':'DELETE','observed':{'name':name}}, {})
        for name in A.M.RETIRED_BOOTSTRAP_FUNCTIONS:
            A.M.validate_delete({'kind':'function','disposition':'DELETE','observed':{'name':name}}, {})
        inventory,draft,plan=prepared()
        with self.assertRaisesRegex(RuntimeError,'operator code drifted'):
            A.sql_for({**plan,'operator_sha256':'0'*64},'1'*64,'2'*64,'3'*64)
        bad=copy.deepcopy(draft);bad['objects'][0]['observed']['name']='content_body'
        with self.assertRaisesRegex(RuntimeError,'identities differ'):
            A.build_plan(inventory,'c'*64,bad,'d'*64,'e'*64)
        inventory['catalog']['indexes']=[{'oid':10,'name':'orphan','relation_oid':42}]
        inventory['before_state_sha256']=A.digest(inventory['catalog'])
        objects=A.M.observed_objects(inventory)
        decisions={item['key']:{'key':item['key'],'disposition':'DELETE' if item['kind']=='index' or item['observed'].get('name')=='fixture' else 'KEEP',
                               'reason':'fixture','authority':'fixture'} for item in objects}
        draft=A.M.draft(inventory,'c'*64,decisions)
        with self.assertRaisesRegex(RuntimeError,'whole relation'):
            A.build_plan(inventory,'c'*64,draft,'d'*64,'e'*64)

    def test_approval_opens_proofs_and_rejects_stale_or_different_state(self):
        _,_,plan=prepared()
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);root.chmod(0o700)
            proof_sha=A.C.private_create(root/'proof.json',{'fixture':'proof'})
            bindings={key:plan[key] for key in ('before_state_sha256','pointer_set_sha256','runtime_package_sha256')}
            references={}
            for name in A.GATES:
                gate={'schema_version':'mainrag.storage-v2.cleanup-gate.v1','gate':name,'status':'PASS',
                      'bindings':bindings,'observed_at_unix':100,'proofs':[{'file':'proof.json','sha256':proof_sha}]}
                if name=='post_activation_and_regular_ingest':gate.update(activation_manifest_sha256='f'*64,regular_ingest_completed=True)
                if name=='dependency_and_caller_review':gate['callers']=[]
                sha=A.C.private_create(root/(name+'.json'),gate)
                references[name]={'file':name+'.json','sha256':sha}
            approval={'schema_version':'mainrag.storage-v2.cleanup-approval.v1','manifest_sha256':'1'*64,
                      'review_kind':'OWNER_AUTHORIZED_SELF_REVIEW','authority':'public fixture owner',
                      'accepts_loss_of_legacy_rollback':True,'observed_at_unix':100,'gates':references}
            self.assertEqual(set(A.validate_approval(plan,'1'*64,approval,root,100)),A.GATES)
            for changed in ({'manifest_sha256':'2'*64},{'accepts_loss_of_legacy_rollback':False},{'observed_at_unix':-1000}):
                with self.subTest(changed=changed),self.assertRaisesRegex(RuntimeError,'authority'):
                    A.validate_approval(plan,'1'*64,{**approval,**changed},root,100)
            (root/'proof.json').write_text('{}');(root/'proof.json').chmod(0o600)
            with self.assertRaisesRegex(RuntimeError,'digest differs'):
                A.validate_approval(plan,'1'*64,approval,root,100)

    def test_runtime_reads_real_process_executable_and_startup_binding(self):
        environment=os.environ.copy()
        environment.update(MAINRAG_STORAGE_V2_DEFAULT_READ_MANIFEST_SHA256='a'*64,
                           MAINRAG_STORAGE_V2_ACTIVE_INGEST_COMMIT_SHA='b'*40,
                           MAINRAG_STORAGE_V2_LEGACY_RETIRED_MANIFEST_SHA256='c'*64)
        child=subprocess.Popen([os.sys.executable,'-c','import time;time.sleep(30)'],env=environment)
        try:
            observed=A.runtime_readback(child.pid)
            gates={'runtime_retirement':{'runtime':observed},
                   'post_activation_and_regular_ingest':{'activation_manifest_sha256':'a'*64}}
            A.validate_runtime({'runtime_package_sha256':observed['binary_sha256']},'c'*64,gates,False)
            with self.assertRaisesRegex(RuntimeError,'drifted'):
                A.validate_runtime({'runtime_package_sha256':observed['binary_sha256']},'d'*64,gates,False)
            gates['runtime_retirement']['runtime']={**observed,'start_ticks':'0'}
            with self.assertRaisesRegex(RuntimeError,'drifted'):
                A.validate_runtime({'runtime_package_sha256':observed['binary_sha256']},'c'*64,gates,False)
        finally:
            child.terminate();child.wait(timeout=5)


if __name__=='__main__':unittest.main()
