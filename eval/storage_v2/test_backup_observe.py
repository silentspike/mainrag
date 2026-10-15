"""Read-only pgBackRest evidence and preflight binding checks."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
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


class LocalLogicalBackupTests(unittest.TestCase):
    """Synthetic policy/artifact cases; these do not claim a real backup."""

    def setUp(self):
        self.local = preflight.LOCAL_BACKUP
        self.root_uid = patch.object(self.local, "ROOT_UID", os.getuid())
        self.root_uid.start()
        self.addCleanup(self.root_uid.stop)

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        root.chmod(0o700)
        packs = root / "packs"
        packs.mkdir(mode=0o700)
        pack = packs / "00000000-0000-0000-0000-000000000001.pack"
        pack.write_bytes(b"synthetic retained pack")
        now = int(time.time())
        database = {"system_identifier": "synthetic", "database": "mainrag", "database_oid": 100}
        metadata = {"database_identity": database, "function_map_sha256": "a" * 64,
                    "packs": [{"pack_id": "00000000-0000-0000-0000-000000000001",
                               "storage_key": pack.name, "manifest_sha256": "b" * 64,
                               "stored_bytes": pack.stat().st_size}]}
        closure = self.local.pack_closure(metadata, packs)
        plan_ref = self.local.create_json(root / "plan.json", {
            "status": "PLANNED_METADATA_ONLY_NOT_A_BACKUP", "metadata": metadata})
        closure_ref = self.local.create_json(root / "pack-closure.json", closure)
        integrity = self.local.create_json(root / "integrity.json", {
            "kind": "mainrag.local-logical-backup.pack-integrity.v1",
            "status": "PASS_ALL_REFERENCED_PACKS_INTEGRITY",
            "pack_closure_sha256": self.local.digest(closure), "pack_count": closure["pack_count"]})
        retention = {"status": "ROOT_APPROVED_LOCAL_LOGICAL_BACKUP_RETENTION", "approved": True,
                     "root_reviewer_uid": os.getuid(), "independent_review_claimed": False,
                     "plan": plan_ref, "pack_closure_sha256": self.local.digest(closure),
                     "preserve_pack_bytes_and_paths": True, "exclude_pack_ids_from_gc_repack_unlink": True,
                     "preserve_backup_artifacts": True, "credential_recovery_external": True,
                     "retain_until_unix": now + 86400, "at_unix": now - 40,
                     "pack_integrity_evidence": integrity}
        retention_ref = self.local.create_json(root / "retention.json", retention)
        artifacts = {}
        for key, name, raw in (("archive", "database.dump", b"PGDMPsynthetic archive"),
                               ("globals", "globals.sql", b"-- synthetic roles; no passwords\n"),
                               ("schema", "schema.sql", b"\\restrict random\n-- schema\n\\unrestrict random\n")):
            target = root / name
            target.write_bytes(raw)
            target.chmod(0o600)
            artifacts[key] = {"file": name, "sha256": hashlib.sha256(raw).hexdigest(),
                              "bytes": len(raw), "stat_identity": self.local.identity(target.stat()), "exit_code": 0}
        receipt = {"schema_version": self.local.SCHEMA, "provider": "pg_dump_custom_local",
                   "status": "COMPLETED_LOCAL_LOGICAL_BACKUP", "source": "local_unix_socket",
                   "plan": plan_ref, "database_identity": database, "server_version_num": 180004,
                   "function_map_sha256": metadata["function_map_sha256"],
                   "schema_sha256": hashlib.sha256(self.local.normalized_schema((root / "schema.sql").read_bytes())).hexdigest(),
                   **artifacts, "external_pack_closure": closure_ref, "retention_decision": retention_ref,
                   "snapshot_consistent": True, "dump_exit_code": 0, "globals_exit_code": 0,
                   "started_at_unix": now - 30, "completed_at_unix": now - 20, "observed_at_unix": now - 20,
                   "restore_tested": False, "self_contained": False, "credential_recovery_external": True,
                   "archive_toc_readable": True, "archive_toc_sha256": "c" * 64}
        evidence = root / "completion.json"
        self.local.create_json(evidence, receipt)
        return SimpleNamespace(root=root, pack=pack, now=now, database=database, receipt=receipt,
                               evidence=evidence, retention=retention)

    def rewrite(self, path, value):
        path.write_bytes(self.local.canonical(value) + b"\n")

    def test_completed_local_backup_dispatch_binds_database_and_remains_unrestored(self):
        fixture = self.fixture()
        result = preflight.load_backup(fixture.evidence, 3600, fixture.now, fixture.database)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["evidence_level"], "local-logical-dump-with-retained-external-packs")
        self.assertFalse(result["restore_tested"])
        self.assertFalse(result["self_contained"])
        self.assertTrue(result["credential_recovery_external"])
        self.assertEqual(preflight.load_backup(fixture.evidence, 1, fixture.now)["status"], "BLOCKED")
        with self.assertRaisesRegex(RuntimeError, "another database"):
            preflight.load_backup(fixture.evidence, 3600, fixture.now, dict(fixture.database, database_oid=101))

    def test_incomplete_failed_exit_and_forged_restore_are_not_backup_acceptance(self):
        for patch_value in ({"status": "PLANNED_METADATA_ONLY_NOT_A_BACKUP"},
                            {"dump_exit_code": 1}, {"restore_tested": True}, {"snapshot_consistent": False}):
            with self.subTest(patch_value=patch_value):
                fixture = self.fixture()
                self.rewrite(fixture.evidence, dict(fixture.receipt, **patch_value))
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    preflight.load_backup(fixture.evidence, 3600, fixture.now)

    def test_tampered_archive_and_symlink_are_rejected(self):
        fixture = self.fixture()
        archive = fixture.root / "database.dump"
        archive.write_bytes(b"PGDMPmodified archive")
        with self.assertRaisesRegex(RuntimeError, "artifact changed"):
            preflight.load_backup(fixture.evidence, 3600, fixture.now)
        fixture = self.fixture()
        archive = fixture.root / "database.dump"
        archive.unlink()
        archive.symlink_to(fixture.root / "globals.sql")
        with self.assertRaisesRegex(RuntimeError, "artifact changed"):
            preflight.load_backup(fixture.evidence, 3600, fixture.now)

    def test_retained_pack_mutation_expiry_or_missing_proof_blocks(self):
        fixture = self.fixture()
        fixture.pack.write_bytes(b"changed retained pack")
        with self.assertRaisesRegex(RuntimeError, "retained backup pack changed"):
            preflight.load_backup(fixture.evidence, 3600, fixture.now)
        fixture = self.fixture()
        fixture.retention["retain_until_unix"] = fixture.now - 1
        self.rewrite(fixture.root / "retention.json", fixture.retention)
        fixture.receipt["retention_decision"] = self.local.reference(fixture.root / "retention.json")
        self.rewrite(fixture.evidence, fixture.receipt)
        with self.assertRaisesRegex(RuntimeError, "retention missing or expired"):
            preflight.load_backup(fixture.evidence, 3600, fixture.now)
        fixture = self.fixture()
        (fixture.root / "integrity.json").unlink()
        with self.assertRaises(FileNotFoundError):
            preflight.load_backup(fixture.evidence, 3600, fixture.now)

    def test_integrity_wrong_closure_missing_scope_or_incomplete_packs_are_rejected(self):
        for change in ({"pack_closure_sha256": "f" * 64}, {"pack_count": None},
                       {"pack_count": 0}, {"pack_ids": []}, {"status": "PASS_SYNTHETIC_POLICY_ONLY"}):
            with self.subTest(change=change):
                fixture = self.fixture()
                integrity_path = fixture.root / "integrity.json"
                integrity = self.local.read_json(integrity_path)
                if change.get("pack_count", False) is None:
                    integrity.pop("pack_count")
                else:
                    integrity.update(change)
                self.rewrite(integrity_path, integrity)
                fixture.retention["pack_integrity_evidence"] = self.local.reference(integrity_path)
                self.rewrite(fixture.root / "retention.json", fixture.retention)
                fixture.receipt["retention_decision"] = self.local.reference(fixture.root / "retention.json")
                self.rewrite(fixture.evidence, fixture.receipt)
                with self.assertRaisesRegex(RuntimeError, "integrity.*(closure|scope)"):
                    preflight.load_backup(fixture.evidence, 3600, fixture.now)

    def test_apply_gate_forwards_actual_database_identity_to_backup_validator(self):
        with patch.dict(sys.modules, {"preflight": preflight}):
            apply_gate = module("local_backup_apply_gate", "ops/storage-v2/apply-gate.py")
        fixture = self.fixture()
        args = SimpleNamespace(database="mainrag", local_postgres=True, backup_evidence=fixture.evidence)
        policy = {"allowed_database_applications": [], "backup_max_age_seconds": 3600}
        snapshot = {"database_identity": fixture.database}
        with patch.object(apply_gate.preflight, "collect_database", return_value=(snapshot, "18", "a" * 64)), \
                patch.object(apply_gate.preflight, "collect_units", return_value={}), \
                patch.object(apply_gate.preflight, "git_state", return_value=("b" * 40, True)), \
                patch.object(apply_gate.preflight, "evaluate", return_value={}), \
                patch.object(apply_gate.preflight, "load_backup", return_value={"status": "PASS"}) as load, \
                patch.object(apply_gate.time, "time", return_value=fixture.now):
            apply_gate.collect(args, policy)
            load.assert_called_once_with(fixture.evidence, 3600, fixture.now, fixture.database)

    def test_stream_output_cap_and_nonzero_exit_remove_only_new_partial(self):
        for returncode, cap in ((0, self.local.COMPLETION_RESERVE + 3), (1, self.local.COMPLETION_RESERVE + 100)):
            with self.subTest(returncode=returncode):
                fixture = self.fixture()
                reader, writer = os.pipe()
                os.write(writer, b"PGDMPsynthetic")
                os.close(writer)
                process = SimpleNamespace(stdout=os.fdopen(reader, "rb"), poll=lambda: returncode,
                                          wait=lambda **_: returncode)
                target = fixture.root / "failed.dump"
                plan = {"maximum_output_bytes": cap, "reserve_bytes": 1}
                with patch.object(self.local.subprocess, "Popen", return_value=process), \
                        patch.object(self.local, "resource_guard", return_value={}):
                    with self.assertRaisesRegex(RuntimeError, "cap exhausted|exited unsuccessfully"):
                        self.local.stream_dump(["synthetic-command-not-executed"], target, plan, {}, time.monotonic() + 5, 0)
                self.assertFalse(target.exists())
                self.assertTrue((fixture.root / "database.dump").exists())
        fixture = self.fixture()
        with self.assertRaisesRegex(RuntimeError, "existing backup attempt"):
            self.local.stream_dump([], fixture.root / "database.dump", {}, {}, time.monotonic() + 5, 0)

    def test_observed_pool_peak_gets_no_credit_from_later_free_space(self):
        mib = self.local.MIB
        origin = {"uuid": "synthetic", "size_bytes": 1000 * mib, "used_bytes": 100 * mib}
        current = dict(origin, used_bytes=106 * mib, metadata_used=1, metadata_total=100)
        plan = {"thin_pool": "synthetic", "filesystem": {"device": 1}, "reserve_bytes": mib,
                "maximum_output_bytes": 20 * mib, "maximum_pool_growth_bytes": 10 * mib}
        with patch.object(self.local, "pool_observation", side_effect=[current, dict(current, used_bytes=104 * mib)]), \
                patch.object(self.local, "local_filesystem", return_value=plan["filesystem"]), \
                patch.object(self.local.os, "statvfs", return_value=SimpleNamespace(f_bavail=100 * mib, f_frsize=1)):
            self.local.resource_guard(plan, Path("/synthetic"), origin, 1)
            self.assertEqual(plan["_observed_peak_pool_growth_bytes"], 6 * mib)
            plan["maximum_pool_growth_bytes"] = 5 * mib
            with self.assertRaisesRegex(RuntimeError, "physical pool reserve exhausted"):
                self.local.resource_guard(plan, Path("/synthetic"), origin, 1)

    def test_schema_normalization_preserves_exact_existing_preflight_rules(self):
        for raw in (b"\\restrict abc\nCREATE TABLE fixture();\n\\unrestrict abc\n",
                    b"\\restrict abc\r\n-- CRLF\r\n\\unrestrict abc"):
            self.assertEqual(self.local.normalized_schema(raw), preflight.normalize_schema_dump(raw))


if __name__ == "__main__":
    unittest.main()
