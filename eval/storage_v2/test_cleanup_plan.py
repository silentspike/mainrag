"""Fail-closed tests for the protected cleanup catalog capture."""

import importlib.util
import json
import stat
import subprocess
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
        "pointer_set_sha256": "a" * 64, "open_reader_count": 0,
        "building_run_count": 0, "generations": [], "packs": [],
        "activation_receipt_relation_oid": None,
        "exact_rows": {},
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
        self.assertIn("'exact_rows', ('{}'::jsonb)", kwargs["input"])
        self.assertNotIn("%TARGET_LOCK_SQL%", kwargs["input"])

        with self.assertRaisesRegex(RuntimeError, "invalid"):
            cleanup.exact_rows_sql(("files; DROP TABLE files",))
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            cleanup.exact_rows_sql(("files", "files"))
        self.assertIn("FROM public.\"files\"", cleanup.exact_rows_sql(("files",)))
        self.assertEqual(cleanup.target_lock_sql(("files",)),
                         'LOCK TABLE public."files" IN ACCESS SHARE MODE;')

        broken = {**catalog_fixture(), "relations": None}
        with patch.object(cleanup.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(broken)
        )):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                cleanup.catalog("fixture", False)
        with patch.object(cleanup.subprocess, "run", return_value=SimpleNamespace(
            returncode=0, stdout=json.dumps(catalog_fixture())
        )):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                cleanup.catalog("fixture", False, ("files",))
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

    def test_qdrant_inventory_binds_exact_counts_and_rejects_alias_drift(self):
        for url in ("http://example.org:6333", "http://127.0.0.1:6333/path",
                    "http://user:password@127.0.0.1:6333"):
            with self.assertRaises(RuntimeError):
                cleanup.qdrant_origin(url)
        with tempfile.TemporaryDirectory() as temporary:
            key_file = Path(temporary) / "key"
            key_file.write_text("fixture-key\n")
            key_file.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "private"):
                cleanup.qdrant_key(key_file)
            key_file.chmod(0o600)
            self.assertEqual(cleanup.qdrant_key(key_file), "fixture-key")

        def response(_origin, _key, path, *, exact_count=False):
            if path == "/collections":
                return {"collections": [{"name": "fixture"}]}
            if path == "/aliases":
                return {"aliases": [{"alias_name": "current", "collection_name": "fixture"}]}
            if path == "/collections/fixture":
                return {"status": "green", "config": {"params": {}}}
            if path == "/collections/fixture/points/count":
                self.assertTrue(exact_count)
                return {"count": 7}
            self.fail(f"unexpected fixture path: {path}")

        with patch.object(cleanup, "qdrant_response", side_effect=response):
            observed = cleanup.qdrant_inventory("http://127.0.0.1:6333", None)
        self.assertEqual(observed["collections"][0]["exact_point_count"], 7)
        self.assertEqual(observed["aliases"][0]["alias_name"], "current")
        self.assertEqual(observed["consistency"], "TWO_LIST_READBACKS_NOT_ATOMIC")

        calls = 0

        def drifted(_origin, _key, path, *, exact_count=False):
            nonlocal calls
            if path == "/aliases":
                calls += 1
                return {"aliases": [] if calls == 2 else [
                    {"alias_name": "current", "collection_name": "fixture"}]}
            return response(_origin, _key, path, exact_count=exact_count)

        with patch.object(cleanup, "qdrant_response", side_effect=drifted):
            with self.assertRaisesRegex(RuntimeError, "drifted"):
                cleanup.qdrant_inventory("http://127.0.0.1:6333", None)

    def test_runtime_search_binds_clean_tracked_code_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def git(*arguments):
                subprocess.run(["git", "-C", str(root), *arguments],
                               check=True, capture_output=True)

            git("init", "-q")
            source = root / "api/src/lib.rs"
            source.parent.mkdir(parents=True)
            source.write_text("const TABLE: &str = \"chunks\";\n")
            git("add", "api/src/lib.rs")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            (root / "ops").mkdir()
            (root / "ops/untracked.txt").write_text("private user work\n")
            observed = cleanup.runtime_inventory(root, ("chunks",))
            self.assertEqual(len(observed["files"]), 1)
            self.assertEqual(observed["matches"][0]["terms"], ["chunks"])
            self.assertNotIn("private user work", json.dumps(observed))
            source.write_text("const TABLE: &str = \"files\";\n")
            with self.assertRaisesRegex(RuntimeError, "clean tracked tree"):
                cleanup.runtime_inventory(root, ("chunks",))


if __name__ == "__main__":
    unittest.main()
