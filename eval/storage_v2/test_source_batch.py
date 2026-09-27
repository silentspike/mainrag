"""Durable source-batch boundaries and recovery checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest


OPERATOR = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                              "ops/storage-v2/source-batch.py"))


class SourceBatchTests(unittest.TestCase):
    def test_stops_after_current_source_then_resumes_same_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_path = root / "plan.json"
            state_path = root / "state.json"
            plan = {
                "schema_version": "mainrag.storage-v2.source-batch-plan.v1",
                "package_commit_sha": "a" * 40,
                "sources": [{
                    "source_id": sid, "adapter": "fs", "failure_group": f"group-{sid}",
                    "planned_items": 3,
                    "steps": [{"name": "review", "tool": "source-review",
                               "args": ["--source-id", str(sid), "--protected-output",
                                        str(root / f"result-{sid}.json")],
                               "result_path": str(root / f"result-{sid}.json")}],
                } for sid in (101, 102)],
            }
            plan_path.write_text(json.dumps(plan))
            os.chmod(plan_path, 0o600)
            args = argparse.Namespace(plan=plan_path, state=state_path, resume=False)
            calls = []

            def fake_invoke(step_plan, path, source, step, state):
                calls.append(source["source_id"])
                step["status"] = "passed"
                OPERATOR["write_state"](path, state)
                if source["source_id"] == 101:
                    path.with_suffix(path.suffix + ".stop").touch(mode=0o600)
                return True

            # runpy functions share their own globals; patch that table directly.
            global_table = OPERATOR["run"].__globals__
            previous = global_table["invoke"]
            try:
                global_table["invoke"] = fake_invoke
                OPERATOR["run"](args)
                state = json.loads(state_path.read_text())
                self.assertEqual(calls, [101])
                self.assertEqual([source["status"] for source in state["sources"]],
                                 ["passed", "pending"])
                args.resume = True
                OPERATOR["run"](args)
                state = json.loads(state_path.read_text())
                self.assertEqual(calls, [101, 102])
                self.assertEqual([source["status"] for source in state["sources"]],
                                 ["passed", "passed"])
            finally:
                global_table["invoke"] = previous

    def test_crashed_phase_requires_reconciliation(self) -> None:
        state = {"sources": [{"status": "running", "steps": [
            {"status": "running", "pid": 999999999,
             "process_start_ticks": "1"}]}]}
        self.assertTrue(OPERATOR["reconcile_crashed_steps"](state))
        self.assertEqual(state["sources"][0]["status"], "needs_reconciliation")
        self.assertEqual(state["sources"][0]["steps"][0]["status"],
                         "needs_reconciliation")


if __name__ == "__main__":
    unittest.main()
