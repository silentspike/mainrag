"""The coupled default switch fails closed before changing service state."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/default-read-switch.py"
SPEC = importlib.util.spec_from_file_location("default_read_switch", PATH)
assert SPEC and SPEC.loader
SWITCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SWITCH)


class DefaultReadSwitchTests(unittest.TestCase):
    def fixture(self, directory: Path, *, age: int = 0) -> tuple[argparse.Namespace, str]:
        now = int(time.time())
        digest = "a" * 64
        binary = directory / "mainrag-api"
        binary.write_bytes(b"reviewed-binary")
        plan = {
            "schema_version": "mainrag.storage-v2.activation-plan.v1",
            "manifest_sha256": digest,
            "installed_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "candidate_set_sha256": "b" * 64,
            "before_pointer_set_sha256": "c" * 64,
            "default_switch": SWITCH.DEFAULT_SWITCH_CONTRACT,
            "manifest": {"activation_id": "00000000-0000-4000-8000-000000000001",
                         "code_commit_sha": "d" * 40,
                         "schema_sha256": "e" * 64,
                         "backend_package_sha256": "f" * 64,
                         "sources": []},
        }
        plan_file = directory / "plan.json"
        SWITCH.OPERATOR.private_write(plan_file, plan)
        plan_sha = hashlib.sha256(plan_file.read_bytes()).hexdigest()
        approval = {
            "schema_version": "mainrag.storage-v2.activation-approval.v1",
            "status": "APPROVED", "plan_sha256": plan_sha,
            "manifest_sha256": digest, "code_commit_sha": "d" * 40,
            "schema_sha256": "e" * 64, "backend_package_sha256": "f" * 64,
            "candidate_set_sha256": "b" * 64,
            "before_pointer_set_sha256": "c" * 64,
            "approved_at_unix": now,
        }
        approval_file = directory / "approval.json"
        SWITCH.OPERATOR.private_write(approval_file, approval)
        attempt = {
            "schema_version": "mainrag.storage-v2.activation-attempt.v1",
            "status": "DB_COMMITTED_DEFAULT_SWITCH_PENDING",
            "plan_sha256": plan_sha, "manifest_sha256": digest,
            "activation_id": plan["manifest"]["activation_id"],
            "committed_at_unix": now - age,
        }
        attempt_file = directory / "attempt.json"
        SWITCH.OPERATOR.private_write(attempt_file, attempt)
        return argparse.Namespace(
            database="fixture", local_postgres=False,
            plan=plan_file, plan_sha256=plan_sha,
            approval=approval_file,
            approval_sha256=hashlib.sha256(approval_file.read_bytes()).hexdigest(),
            attempt=attempt_file,
            attempt_sha256=hashlib.sha256(attempt_file.read_bytes()).hexdigest(),
            api_url="http://127.0.0.1:3001", api_token_file=None,
            output=directory / "switch.json",
        ), digest

    def test_stale_commit_never_changes_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args, _ = self.fixture(directory, age=301)
            with patch.object(SWITCH, "DROPIN", directory / "unit.conf"), \
                 patch.object(SWITCH, "ENV_FILE", directory / "selector.env"), \
                 patch.object(SWITCH, "API_BINARY", directory / "mainrag-api"), \
                 patch.object(SWITCH, "systemctl") as systemctl:
                with self.assertRaisesRegex(RuntimeError, "immediately coupled"):
                    SWITCH.switch(args)
                systemctl.assert_not_called()
                self.assertFalse(args.output.exists())

    def test_switch_binds_commit_service_and_api_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args, digest = self.fixture(directory)
            with patch.object(SWITCH, "DROPIN", directory / "unit.conf"), \
                 patch.object(SWITCH, "ENV_FILE", directory / "selector.env"), \
                 patch.object(SWITCH, "API_BINARY", directory / "mainrag-api"), \
                 patch.object(SWITCH.OPERATOR, "committed_readback", return_value={}), \
                 patch.object(SWITCH.OPERATOR, "verify_committed") as verified, \
                 patch.object(SWITCH, "service_binary_sha256",
                              return_value=hashlib.sha256(b"reviewed-binary").hexdigest()), \
                 patch.object(SWITCH, "systemctl") as systemctl, \
                 patch.object(SWITCH, "read_service_state",
                              side_effect=[(10, "", ""), (11, digest, "d" * 40)]), \
                 patch.object(SWITCH, "api_read_path",
                              side_effect=["current", "storage_v2_active"]):
                result = SWITCH.switch(args)
            self.assertEqual(result["status"], "DEFAULT_SWITCHED_POST_INGEST_PENDING")
            self.assertEqual((directory / "selector.env").read_text(),
                             SWITCH.ENV_NAME + "=" + digest + "\n"
                             + SWITCH.ACTIVE_COMMIT_ENV_NAME + "=" + "d" * 40 + "\n")
            self.assertIn("EnvironmentFile=", (directory / "unit.conf").read_text())
            self.assertEqual(verified.call_count, 2)
            systemctl.assert_any_call("restart", SWITCH.UNIT)
            args.output = directory / "retry.json"
            with patch.object(SWITCH, "DROPIN", directory / "unit.conf"), \
                 patch.object(SWITCH, "ENV_FILE", directory / "selector.env"), \
                 patch.object(SWITCH, "API_BINARY", directory / "mainrag-api"), \
                 patch.object(SWITCH.OPERATOR, "committed_readback", return_value={}), \
                 patch.object(SWITCH.OPERATOR, "verify_committed"), \
                 patch.object(SWITCH, "service_binary_sha256",
                              return_value=hashlib.sha256(b"reviewed-binary").hexdigest()), \
                 patch.object(SWITCH, "systemctl") as retry_systemctl, \
                 patch.object(SWITCH, "read_service_state",
                              return_value=(11, digest, "d" * 40)), \
                 patch.object(SWITCH, "api_read_path", return_value="storage_v2_active"):
                retried = SWITCH.switch(args)
            self.assertEqual(retried["status"], "DEFAULT_SWITCHED_POST_INGEST_PENDING")
            retry_systemctl.assert_not_called()

    def test_running_binary_mismatch_stops_before_switch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args, _ = self.fixture(directory)
            with patch.object(SWITCH, "DROPIN", directory / "unit.conf"), \
                 patch.object(SWITCH, "ENV_FILE", directory / "selector.env"), \
                 patch.object(SWITCH, "API_BINARY", directory / "mainrag-api"), \
                 patch.object(SWITCH.OPERATOR, "committed_readback", return_value={}), \
                 patch.object(SWITCH.OPERATOR, "verify_committed"), \
                 patch.object(SWITCH, "read_service_state", return_value=(10, "", "")), \
                 patch.object(SWITCH, "service_binary_sha256", return_value="0" * 64), \
                 patch.object(SWITCH, "systemctl") as systemctl:
                with self.assertRaisesRegex(RuntimeError, "running API binary differs"):
                    SWITCH.switch(args)
            systemctl.assert_not_called()
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
