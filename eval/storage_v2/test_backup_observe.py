"""Read-only pgBackRest evidence and preflight binding checks."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


backup = module("backup_observe", "ops/storage-v2/backup-observe.py")
preflight = module("preflight_backup_binding", "ops/storage-v2/preflight.py")


class BackupObservationTests(unittest.TestCase):
    def inventory(self, now: int) -> bytes:
        return json.dumps([{"name": "fixture", "status": {
                                "code": 0, "lock": {"backup": {"held": False},
                                                    "restore": {"held": False}}},
                            "repo": [{"status": {"code": 0}}],
                            "backup": [{"label": "fixture-backup", "type": "full",
                                        "error": False,
                                        "timestamp": {"stop": now - 60}}]}]).encode()

    def test_capture_binds_private_metadata_and_never_claims_restore(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            info = root / "info.json"
            evidence = root / "evidence.json"
            with patch.object(backup.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=self.inventory(now)
            )):
                captured = backup.capture("fixture", info, evidence)
            self.assertFalse(captured["restore_tested"])
            self.assertEqual(info.stat().st_mode & 0o777, 0o600)
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
            checked = preflight.load_backup(evidence, 3600, now)
            self.assertEqual(checked["status"], "PASS")
            self.assertEqual(checked["evidence_level"], "backup-metadata-only")
            self.assertFalse(checked["restore_tested"])
            self.assertEqual(preflight.load_backup(evidence, 1, now)["status"], "BLOCKED")
            info.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "digest differs"):
                preflight.load_backup(evidence, 3600, now)

    def test_failed_backup_and_forged_restore_are_rejected(self) -> None:
        now = int(time.time())
        bad = json.loads(self.inventory(now))
        bad[0]["backup"][0]["error"] = True
        with self.assertRaisesRegex(RuntimeError, "error-free"):
            backup.latest_backup(json.dumps(bad).encode(), "fixture", now)
        bad[0]["backup"][0]["error"] = False
        bad[0]["status"]["lock"]["backup"]["held"] = True
        with self.assertRaisesRegex(RuntimeError, "lock state"):
            backup.latest_backup(json.dumps(bad).encode(), "fixture", now)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            root.mkdir(mode=0o700)
            info = root / "info.json"
            evidence = root / "evidence.json"
            info.write_bytes(self.inventory(now))
            info.chmod(0o600)
            observed = backup.latest_backup(info.read_bytes(), "fixture", now)
            forged = {"schema_version": 2, "status": "PASS", "stanza": "fixture",
                      "artifact_file": info.name,
                      "artifact_sha256": backup.hashlib.sha256(info.read_bytes()).hexdigest(),
                      **observed, "restore_tested": True, "observed_at_unix": now}
            evidence.write_text(json.dumps(forged))
            evidence.chmod(0o600)
            with self.assertRaisesRegex(RuntimeError, "unsupported schema"):
                preflight.load_backup(evidence, 3600, now)


if __name__ == "__main__":
    unittest.main()
