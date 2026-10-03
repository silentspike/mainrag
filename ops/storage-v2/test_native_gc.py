"""Negative operator admission checks independent of SQL implementation."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('gc_operator_test',Path(__file__).with_name('native-gc.py'))
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


class NativeGcAdmissionTests(unittest.TestCase):
    def test_retention_requires_real_hash_bound_proof_and_exact_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sha = G.A.C.private_create(root/'export-proof.json',{'status':'PUBLIC_FIXTURE_EXPORT_INVENTORY'})
            value = {'schema_version':'mainrag.storage-v2.gc-retention.v1','preserve_all_generations':True,
                     'authority':'public fixture reviewer','roots':[{'table':'content_body','id':7}],
                     'proofs':[{'file':'export-proof.json','sha256':sha}]}
            G.retention(value,root)
            for roots in ([{'table':'sources','id':7}],[{'table':'content_body','id':False}],
                          [value['roots'][0],value['roots'][0]]):
                with self.subTest(roots=roots), self.assertRaises(RuntimeError):
                    G.retention(dict(value,roots=roots),root)
            (root/'export-proof.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError,'digest differs'):
                G.retention(value,root)

    def test_missing_export_proof_is_not_an_empty_retention_decision(self):
        value = {'schema_version':'mainrag.storage-v2.gc-retention.v1','preserve_all_generations':True,
                 'authority':'public fixture reviewer','roots':[],'proofs':[]}
        with self.assertRaisesRegex(RuntimeError,'proofs are required'):
            G.retention(value,Path('.'))

    def test_resource_admission_rejects_wal_pressure_and_deleted_row_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            policy = {'data_filesystem':directory,'root_filesystem':directory,'pack_root':directory,
                      'thinpool_uuid':'public-fixture-pool'}
            plan = {'resource_policy':policy,'before':{'tables':[{'total':1,'kept':0,'dead_bytes':100}], 'dependents':[]}}
            lvs = {'report':[{'lv':[{'lv_uuid':'public-fixture-pool','lv_size':str(2*1024**4),
                                    'data_percent':'30','metadata_percent':'10'}]}]}
            with patch.object(G.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(lvs))), \
                 patch.object(G.shutil,'disk_usage',return_value=SimpleNamespace(free=1024**4)), \
                 patch.object(G,'sql',return_value='0') as query:
                admitted = G.resource_readback(plan,'public_fixture',False)
                self.assertGreater(admitted['wal_admission_bytes'],0)
                query.return_value=str(25*1024**3)
                with self.assertRaisesRegex(RuntimeError,'WAL admission'):
                    G.resource_readback(plan,'public_fixture',False)
                G.resource_readback(plan,'public_fixture',False,after=True)
                query.return_value='0'
                plan['before']['tables'][0]['dead_bytes']=5*1024**3
                with self.assertRaisesRegex(RuntimeError,'WAL admission'):
                    G.resource_readback(plan,'public_fixture',False)

    def test_missing_resource_policy_never_admits_gc(self):
        with self.assertRaisesRegex(RuntimeError,'resource policy'):
            G.resource_readback({'resource_policy':None},'public_fixture',False)

    def test_stale_or_unbound_approval_never_reads_runtime(self):
        plan = {key:'a'*64 for key in ('before_state_sha256','activation_manifest_sha256',
                'legacy_cleanup_manifest_sha256','runtime_package_sha256','maintenance_binary_sha256')}
        with patch.object(G.A,'runtime_readback') as runtime, self.assertRaisesRegex(RuntimeError,'fresh exact GC approval'):
            G.validate_approval(plan,'b'*64,{'schema_version':'mainrag.storage-v2.native-gc-approval.v1'},Path('.'),False)
        runtime.assert_not_called()


if __name__=='__main__':
    unittest.main()
