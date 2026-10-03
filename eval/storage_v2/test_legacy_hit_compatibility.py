"""Operator resumes an unknown commit without duplicate work or blind restart."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH=Path(__file__).resolve().parents[2]/'ops/storage-v2/legacy-hit-compatibility.py'
SPEC=importlib.util.spec_from_file_location('legacy_hit_compatibility',PATH)
M=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

class LegacyHitOperatorTests(unittest.TestCase):
    def fixture(self,root):
        package={'commit_sha':'a'*40,'binary_sha256':'b'*64,'installation_receipt_sha256':'c'*64}
        source=dict(source_id=1,generation_id=2,legacy_epoch=0,
            files=[dict(file_id=11,file_sha256='d'*64,legacy_revision=0,hit_count=4,completed_hits=0)])
        plan=dict(schema_version='mainrag.storage-v2.legacy-hit-plan.v1',package=package,include_test=False,
            source_spool_budget_bytes=1024,maximum_growth_bytes=4096,sources=[source])
        args=SimpleNamespace(api_url='http://fixture.invalid',include_test=False,source_spool_budget_bytes=1024,
            maximum_growth_bytes=4096,apply='e'*64,state=root/'state.json')
        return args,plan,package,source

    def test_unknown_commit_observes_live_lease_and_reuses_committed_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            args,plan,package,source=self.fixture(Path(temporary))
            committed=set();writes=[];attempts=[];progress=[]
            def request(origin,token,method,path,body,**kwargs):
                if path.endswith('-inventory'):
                    if body['after_file_id']:return dict(source_id=1,generation_id=2,legacy_epoch=0,files=[])
                    return dict(source_id=1,generation_id=2,legacy_epoch=0,
                        files=[dict(source['files'][0],completed_hits=len(committed))])
                if path.endswith('-progress'):
                    progress.append(len(progress))
                    if len(progress)==1:return dict(source_id=1,generation_id=2,file_id=11,producer_running=True,completed_hits=0)
                    committed.update([1,2]);writes.extend([1,2])
                    return dict(source_id=1,generation_id=2,file_id=11,producer_running=False,completed_hits=2)
                attempts.append(dict(body))
                if len(attempts)==1:raise TimeoutError('fixture response was lost while server work remained live')
                start=body['after_hit_id'];end=min(start+2,4)
                new=[hit for hit in range(start+1,end+1) if hit not in committed]
                committed.update(new);writes.extend(new)
                return dict(source_id=1,generation_id=2,file_id=11,after_hit_id=end,file_done=end==4,processed=len(new))
            with patch.object(M.R,'request',side_effect=request),patch.object(M,'capacity',return_value={'fixture':True}),\
                 patch.object(M.R,'observe_reader_package',return_value=package),patch.object(M.time,'sleep'):
                M.apply(args,'opaque-fixture-token',plan,package)
            self.assertEqual(attempts[0],attempts[1])
            self.assertEqual([attempt['after_hit_id'] for attempt in attempts],[0,0,2])
            self.assertEqual(writes,[1,2,3,4])
            self.assertEqual(len(progress),2)
            state=json.loads(args.state.read_text())
            self.assertEqual(state['status'],'COMPLETE_FOR_PLAN_NOT_CLEANUP_AUTHORITY')
            self.assertEqual(state['completed_hits'],4)

    def test_definite_failure_is_durable_and_cannot_be_retried_blindly(self):
        with tempfile.TemporaryDirectory() as temporary:
            args,plan,package,source=self.fixture(Path(temporary))
            writes=[]
            def request(origin,token,method,path,body,**kwargs):
                if path.endswith('-inventory'):
                    return dict(source_id=1,generation_id=2,legacy_epoch=0,files=[] if body['after_file_id'] else source['files'])
                writes.append(body);raise RuntimeError('API request failed with HTTP 400')
            with patch.object(M.R,'request',side_effect=request),patch.object(M,'capacity',return_value={'fixture':True}):
                with self.assertRaisesRegex(RuntimeError,'HTTP 400'):M.apply(args,'opaque-fixture-token',plan,package)
                self.assertEqual(json.loads(args.state.read_text())['status'],'FAILED_REQUEST')
                with self.assertRaisesRegex(RuntimeError,'definitively rejected'):M.apply(args,'opaque-fixture-token',plan,package)
            self.assertEqual(len(writes),1)

if __name__=='__main__':unittest.main()
