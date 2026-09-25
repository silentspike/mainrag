"""Fail-closed tests for the protected cleanup catalog capture."""

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/cleanup-plan.py"
SPEC = importlib.util.spec_from_file_location("cleanup_plan", MODULE_PATH)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


def catalog_fixture():
    return {
        "database_oid": "1", "relations": [], "columns": [], "constraints": [],
        "policies": [], "triggers": [], "functions": [], "indexes": [],
        "dependencies": [], "active_pointer_count": 0,
        "activation_receipt_relation_oid": None,
    }


class CleanupPlanCaptureTests(unittest.TestCase):
    def test_database_capture_is_read_only_and_rejects_partial_json(self):
        calls = []

        def invoke(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout=json.dumps(catalog_fixture()))

        with patch.object(cleanup.subprocess, "run", side_effect=invoke):
            self.assertEqual(cleanup.catalog("fixture", True), catalog_fixture())
        command, kwargs = calls[0]
        self.assertEqual(command[:4], ["sudo", "-n", "-u", "postgres"])
        self.assertIn("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY", kwargs["input"])
        self.assertIn("default_transaction_read_only=on", kwargs["env"]["PGOPTIONS"])

        broken = {**catalog_fixture(), "relations": None}
        with patch.object(cleanup.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(broken)
        )):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                cleanup.catalog("fixture", False)
        with patch.object(cleanup.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout='{"database_oid":"1","database_oid":"2"}'
        )):
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                cleanup.catalog("fixture", False)

    def test_protected_create_does_not_overwrite_or_use_public_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir(mode=0o700)
            output = private / "catalog.json"
            digest = cleanup.private_create(output, {"status": "OBSERVED_ONLY"})
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(digest, cleanup.hashlib.sha256(output.read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):
                cleanup.private_create(output, {"status": "CHANGED"})
            self.assertEqual(json.loads(output.read_text())["status"], "OBSERVED_ONLY")

            public = root / "public"
            public.mkdir(mode=0o755)
            with self.assertRaisesRegex(RuntimeError, "accessible"):
                cleanup.private_create(public / "catalog.json", {})
            self.assertFalse((public / "catalog.json").exists())


if __name__ == "__main__":
    unittest.main()
