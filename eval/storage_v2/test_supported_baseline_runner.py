"""Shell coordinator contract tests; stub Cargo is not ingestion evidence."""

import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

from eval.storage_v2 import harness
from eval.storage_v2.test_supported_baseline import observation


class SupportedBaselineRunnerTests(unittest.TestCase):
    def run_stub(self, cargo_exit=0, existing=False, dirty=False):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cargo = directory / "cargo"
            cargo.write_text("#!/bin/sh\nprintf '%s\\n' \"$FIXTURE_LOG\"\nexit \"$FIXTURE_EXIT\"\n")
            cargo.chmod(0o700)
            git = directory / "git"
            git.write_text('#!/bin/sh\nif [ "$1" = diff ]; then exit "$FIXTURE_DIRTY"; fi\nexec "'+shutil.which("git")+'" "$@"\n')
            git.chmod(0o700)
            output = directory / "report.json"
            if existing:
                output.write_text("owned sentinel")
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=harness.ROOT, text=True).strip()
            env = dict(os.environ, PATH=str(directory)+os.pathsep+os.environ["PATH"],
                       TMPDIR=str(directory), FIXTURE_EXIT=str(cargo_exit),
                       FIXTURE_DIRTY="1" if dirty else "0",
                       FIXTURE_LOG="corpus baseline: "+json.dumps(observation())+"\ntest result: ok. 1 passed; 0 failed; 0 ignored;",
                       MAINRAG_INDEX_TEST_DATABASE_URL="synthetic-unused-connection",
                       TOKENIZER_ASSET_PATH="synthetic-unused-asset")
            result = subprocess.run(
                ["bash", "eval/storage_v2/run_supported_baseline.sh", str(output), sha, "hosted-ci-local-postgres"],
                cwd=harness.ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(list(directory.glob("tmp.*")), [], "owned capture directories must be removed")
            return result.returncode, output.read_text() if output.exists() else None

    def test_one_command_emits_valid_comparison_and_cleans_captures(self):
        code, text = self.run_stub()
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["runs"]), 2)
        self.assertEqual(report["maintenance_gate"]["status"], "PASS")

    def test_command_failure_cannot_hide_behind_successful_partial_stdout(self):
        code, text = self.run_stub(cargo_exit=7)
        self.assertNotEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("fixture command failed despite partial output", report["differences"])

    def test_existing_output_is_preserved(self):
        code, text = self.run_stub(existing=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(text, "owned sentinel")

    def test_dirty_tracked_code_cannot_claim_the_checkout_commit(self):
        code, text = self.run_stub(dirty=True)
        self.assertNotEqual(code, 0)
        self.assertIsNone(text)
