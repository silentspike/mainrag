"""Exercise live observation and failure draining without a production writer."""
import importlib.util
import json
from argparse import Namespace
from pathlib import Path
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("build_progress", ROOT / "ops/storage-v2/build_progress.py")
MODULE=importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


class BuildProgressTests(unittest.TestCase):
    def test_live_cursor_is_attempt_bound_and_mismatch_drains_writer(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary:
                done=threading.Event()
                args=Namespace(api_url="fixture",source_id=7,commit_sha="a"*40,resume_run_id=99,
                               checkpoint=Path(temporary)/"checkpoint.json")
                identity={}
                def request(_api,_token,method,_path,body=None,**_kwargs):
                    if method=="POST":
                        identity.update(body)
                        if not done.wait(2): raise RuntimeError("fixture monitor did not drain")
                        return {"generation_id":12,"item_count":32}
                    done.set()
                    return {"schema_version":"mainrag.storage-v2.build-progress.v1",
                        "source_id":8 if mismatch else 7,"commit_sha":args.commit_sha,
                        "attempt_id":identity["progress_id"],"phase":"staging",
                        "staged_items":32,"planned_items":100,"elapsed_seconds":1.0,
                        "db_staging_ms":500.0,"transaction_committed":False}
                def write(path,value,**_kwargs): path.write_text(json.dumps(value))
                if mismatch:
                    with self.assertRaisesRegex(RuntimeError,"build drained"):
                        MODULE.monitored_build(args,"fixture-only",request,write,interval=0.01)
                else:
                    result,attempt=MODULE.monitored_build(args,"fixture-only",request,write,interval=0.01)
                    self.assertEqual(result["generation_id"],12)
                    self.assertEqual(attempt,identity["progress_id"])
                state=json.loads(Path(str(args.checkpoint)+".progress.json").read_text())
                self.assertEqual(identity["resume_run_id"],99)
                self.assertTrue(done.is_set())
                self.assertTrue(state["transaction_committed"])
                self.assertEqual(state["generation_id"],12)
                if not mismatch: self.assertFalse(state["progress"]["transaction_committed"])
                with self.assertRaisesRegex(RuntimeError,"attempt exists"):
                    MODULE.monitored_build(args,"fixture-only",request,write,interval=0.01)

    def test_negative_or_nonfinite_progress_is_rejected(self):
        value={"schema_version":"mainrag.storage-v2.build-progress.v1","source_id":7,
            "commit_sha":"a"*40,"attempt_id":"fixture","phase":"staging",
            "staged_items":0,"planned_items":1,"elapsed_seconds":1.0,
            "db_staging_ms":0.0,"transaction_committed":False}
        for changed in ({"staged_items":2},{"staged_items":-1},{"elapsed_seconds":float("nan")},
                        {"transaction_committed":1}):
            with self.assertRaises(ValueError):
                MODULE.validate_observation({**value,**changed},7,"a"*40,"fixture")

    def test_fast_failed_post_retains_backend_failure_and_uncommitted_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            args=Namespace(api_url="fixture",source_id=7,commit_sha="a"*40,
                           checkpoint=Path(temporary)/"checkpoint.json")
            identity={}
            methods=[]
            def request(_api,_token,method,_path,body=None,**_kwargs):
                methods.append(method)
                if method == "POST":
                    identity.update(body)
                    raise RuntimeError("HTTP 500")
                return {"schema_version":"mainrag.storage-v2.build-progress.v1",
                    "source_id":7,"commit_sha":args.commit_sha,
                    "attempt_id":identity["progress_id"],"phase":"staging",
                    "status":"failed_requires_reconciliation",
                    "staged_items":8768,"committed_items":0,"planned_items":45742,
                    "elapsed_seconds":600.0,"db_staging_ms":375000.0,
                    "transaction_committed":False,
                    "failure":{"category":"database_resource_exhausted","sqlstate":"53200"}}
            def write(path,value,**_kwargs): path.write_text(json.dumps(value))
            with self.assertRaisesRegex(RuntimeError,"HTTP 500"):
                MODULE.monitored_build(args,"fixture-only",request,write,interval=0.01)
            state=json.loads(Path(str(args.checkpoint)+".progress.json").read_text())
            self.assertEqual(methods,["POST","GET"])
            self.assertEqual(state["progress"]["failure"]["sqlstate"],"53200")
            self.assertEqual(state["progress"]["committed_items"],0)
            self.assertEqual(state["status"],"request_failed_requires_reconciliation")
