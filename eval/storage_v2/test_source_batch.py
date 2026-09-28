"""Durable source-batch boundaries and recovery checks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
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
                self.assertEqual(OPERATOR["run"](args), 2)
                state = json.loads(state_path.read_text())
                self.assertEqual(calls, [101])
                self.assertEqual([source["status"] for source in state["sources"]],
                                 ["passed", "pending"])
                stopped = subprocess.run([sys.executable, OPERATOR["__file__"], "run",
                    "--plan", str(plan_path), "--state", str(state_path)], capture_output=True)
                self.assertEqual(stopped.returncode, 2)
                args.resume = True
                self.assertEqual(OPERATOR["run"](args), 0)
                state = json.loads(state_path.read_text())
                self.assertEqual(calls, [101, 102])
                self.assertEqual([source["status"] for source in state["sources"]],
                                 ["passed", "passed"])
            finally:
                global_table["invoke"] = previous

    def test_failed_group_does_not_hide_failure_or_block_independent_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = {"schema_version": "mainrag.storage-v2.source-batch-plan.v1",
                    "package_commit_sha": "a" * 40, "sources": [{
                "source_id": sid, "adapter": "fs", "failure_group": group,
                "planned_items": 1, "steps": [{"name": "review", "tool": "source-review",
                    "args": ["--source-id", str(sid), "--protected-output", str(root / f"{sid}.json")],
                    "result_path": str(root / f"{sid}.json")}],
            } for sid, group in [(101, "shared"), (102, "shared"), (103, "independent")]]}
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan))
            plan_path.chmod(0o600)
            state_path = root / "state.json"
            args = argparse.Namespace(plan=plan_path, state=state_path, resume=False)
            calls = []
            def invoke(step_plan, path, source, step, state):
                calls.append(source["source_id"])
                step["status"] = "failed" if source["source_id"] == 101 else "passed"
                source["status"] = step["status"]
                return source["status"] == "passed"
            globals_ = OPERATOR["run"].__globals__
            previous = globals_["invoke"]
            try:
                globals_["invoke"] = invoke
                self.assertEqual(OPERATOR["run"](args), 1)
                failed = subprocess.run([sys.executable, OPERATOR["__file__"], "run",
                    "--plan", str(plan_path), "--state", str(state_path)], capture_output=True)
                self.assertEqual(failed.returncode, 1)
                self.assertEqual(json.loads(failed.stdout)["status"], "FAIL")
                self.assertEqual(calls, [101, 103])
                self.assertEqual([s["status"] for s in json.loads(state_path.read_text())["sources"]],
                                 ["failed", "skipped", "passed"])
                self.assertEqual(OPERATOR["run"](args), 1)
            finally:
                globals_["invoke"] = previous

    def test_projection_phase_requires_complete_verification_without_qualification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); root.chmod(0o700)
            output = root / "state.json"
            step = {"name": "project", "tool": "candidate-projections", "args": [
                "apply", "--source-id", "101", "--state", str(output)], "result_path": str(output)}
            plan = {"schema_version": "mainrag.storage-v2.source-batch-plan.v1", "package_commit_sha": "a" * 40,
                    "sources": [{"source_id": 101, "adapter": "fs", "failure_group": "owned", "planned_items": 1, "steps": [step]}]}
            OPERATOR["validate_plan"](plan)
            value = {"source_id": 101, "status": "PASS_PROJECTIONS_ONLY", "qualification": False,
                     "pending": None, "verification": {"invalid_count": 0, "missing_count": 0}}
            OPERATOR["write_state"](output, value)
            self.assertEqual(OPERATOR["validate_result"](step, output, 101, "a" * 40)[0], value)
            for changes in [{"qualification": True}, {"pending": [{"occurrence_id": 1}]},
                            {"verification": {"invalid_count": 0, "missing_count": 1}}]:
                OPERATOR["write_state"](output, {**value, **changes})
                with self.assertRaises(ValueError):
                    OPERATOR["validate_result"](step, output, 101, "a" * 40)
            step["args"][0] = "plan"
            with self.assertRaises(RuntimeError):
                OPERATOR["validate_plan"](plan)

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
