#!/usr/bin/env python3
"""Unit tests for the storage-v2 operational preflight boundary."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT_PATH = ROOT / "ops" / "storage-v2" / "preflight.py"
APPLY_PATH = ROOT / "ops" / "storage-v2" / "apply-gate.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load_module("storage_v2_preflight", PREFLIGHT_PATH)
sys.path.insert(0, str(PREFLIGHT_PATH.parent))
apply_gate = load_module("storage_v2_apply_gate", APPLY_PATH)
POLICY = json.loads((ROOT / "ops/storage-v2/preflight-policy.json").read_text())


def passing_snapshot() -> dict:
    tuning = preflight.tuning_expectations(ROOT / POLICY["tuning_config"])
    settings = {name: tuning[name] for name in POLICY["required_settings"]}
    return {
        "server_version": "18.4",
        "server_version_num": 180004,
        "settings": settings,
        "extensions": [{"name": "vector", "version": "0.8.2"}],
        "collation": {
            "database_stored_version": "2.43",
            "database_actual_version": "2.43",
            "database_mismatch": False,
            "explicit_mismatch_count": 0,
            "potentially_affected_index_count": 0,
        },
        "activity": {
            "active_writer_count": 0,
            "unknown_application_count": 0,
            "active_index_build_count": 0,
            "active_vacuum_count": 0,
            "active_backfill_count": 0,
        },
        "capacity": {
            "database_bytes": 1_000_000,
            "index_bytes": 500_000,
            "wal_bytes": 100_000,
            "filesystem_free_bytes": 30_000_000_000,
        },
        "backend": {"required_index_count": 2, "valid_required_index_count": 2},
    }


def passing_units() -> dict:
    return {
        "known_unit_count": 6,
        "loaded_unit_count": 6,
        "active_writer_count": 0,
        "active_writer_identity_sha256": "0" * 64,
        "unknown_runtime_count": 0,
        "unknown_runtime_identity_sha256": "0" * 64,
        "timer_count": 2,
        "active_timer_count": 2,
        "active_timer_identity_sha256": "0" * 64,
        "read_behavior_counts": {"available": 1, "unavailable": 1, "unchanged": 4},
        "discovery_failed": False,
        "state_sha256": "1" * 64,
    }


def backup(restore_tested: bool = False) -> dict:
    return {
        "status": "PASS",
        "evidence_level": "restore-exercised" if restore_tested else "backup-command-only",
        "restore_tested": restore_tested,
        "age_seconds": 60,
        "artifact_sha256": "2" * 64,
        "evidence_sha256": "3" * 64,
    }


def evaluate(snapshot: dict | None = None, units: dict | None = None, backup_value: dict | None = None):
    return preflight.evaluate(
        snapshot or passing_snapshot(),
        "18.4",
        "4" * 64,
        units or passing_units(),
        backup_value or backup(),
        POLICY,
        commit_sha="5" * 40,
        tracked_clean=True,
    )


class PreflightTests(unittest.TestCase):
    def test_schema_dump_identity_ignores_only_postgres_safety_token(self) -> None:
        first = b"-- schema\n\\restrict alpha\nCREATE TABLE t(id int);\n\\unrestrict alpha\n"
        second = b"-- schema\n\\restrict beta\nCREATE TABLE t(id int);\n\\unrestrict beta\n"
        changed = b"-- schema\n\\restrict beta\nCREATE TABLE t(id bigint);\n\\unrestrict beta\n"
        self.assertEqual(
            preflight.normalize_schema_dump(first), preflight.normalize_schema_dump(second)
        )
        self.assertNotEqual(
            preflight.normalize_schema_dump(first), preflight.normalize_schema_dump(changed)
        )

    def test_complete_fixture_passes_without_claiming_restore(self) -> None:
        result = evaluate()
        self.assertEqual(result["overall_status"], "PASS")
        self.assertEqual(result["backup"]["evidence_level"], "backup-command-only")
        encoded = json.dumps(result)
        self.assertNotIn("/data/", encoded)
        self.assertNotIn("hostname", encoded)

    def test_manifest_matches_public_schema(self) -> None:
        import jsonschema

        result = evaluate()
        result["generated_at_unix"] = int(time.time())
        schema = json.loads((ROOT / "ops/storage-v2/preflight.schema.json").read_text())
        jsonschema.validate(result, schema)

    def test_version_and_collation_drift_block(self) -> None:
        snapshot = passing_snapshot()
        snapshot["server_version"] = "18.3"
        snapshot["collation"]["database_mismatch"] = True
        snapshot["collation"]["potentially_affected_index_count"] = 7
        result = evaluate(snapshot)
        self.assertEqual(result["overall_status"], "BLOCKED")
        self.assertEqual(result["checks"]["postgresql_version"], "BLOCKED")
        self.assertEqual(result["checks"]["collation"], "BLOCKED")

    def test_unknown_writer_and_active_operation_block(self) -> None:
        snapshot = passing_snapshot()
        snapshot["activity"]["active_writer_count"] = 1
        snapshot["activity"]["active_index_build_count"] = 1
        units = passing_units()
        units["unknown_runtime_count"] = 1
        result = evaluate(snapshot, units)
        self.assertEqual(result["checks"]["maintenance"], "BLOCKED")
        self.assertEqual(result["checks"]["active_operations"], "BLOCKED")

    def test_low_space_and_wrong_backend_digest_inputs_block(self) -> None:
        snapshot = passing_snapshot()
        snapshot["capacity"]["filesystem_free_bytes"] = 1
        snapshot["backend"]["valid_required_index_count"] = 1
        result = evaluate(snapshot)
        self.assertEqual(result["checks"]["capacity"], "BLOCKED")
        self.assertEqual(result["checks"]["backend_index"], "BLOCKED")

    def test_state_identity_ignores_safe_capacity_noise_but_binds_gate_crossing(self) -> None:
        first = evaluate()
        noisy = passing_snapshot()
        noisy["capacity"]["filesystem_free_bytes"] += 123_456_789
        noisy["capacity"]["wal_bytes"] += 987_654
        second = evaluate(noisy)
        self.assertEqual(first["state_sha256"], second["state_sha256"])

        blocked = passing_snapshot()
        blocked["capacity"]["filesystem_free_bytes"] = 1
        third = evaluate(blocked)
        self.assertNotEqual(first["state_sha256"], third["state_sha256"])

    def test_wrong_accepted_backend_digest_fails_closed(self) -> None:
        policy = deepcopy(POLICY)
        policy["accepted_backend_lock_sha256"] = "0" * 64
        result = preflight.evaluate(
            passing_snapshot(), "18.4", "4" * 64, passing_units(), backup(), policy,
            commit_sha="5" * 40, tracked_clean=True,
        )
        self.assertEqual(result["checks"]["backend_lock"], "FAIL")
        self.assertEqual(result["overall_status"], "FAIL")

    def test_missing_backup_is_not_pass(self) -> None:
        result = evaluate(backup_value={
            "status": "BLOCKED",
            "evidence_level": "none",
            "restore_tested": False,
            "reason": "backup evidence was not supplied",
        })
        self.assertEqual(result["checks"]["backup"], "BLOCKED")

    def test_apply_rejects_state_drift(self) -> None:
        before = evaluate()
        checked = dict(before)
        checked["state_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "drifted"):
            apply_gate.validate_before(checked, before, "collation-refresh")

    def test_apply_rejects_failed_prerequisite_and_completed_target(self) -> None:
        before = evaluate()
        with self.assertRaisesRegex(ValueError, "already PASS"):
            apply_gate.validate_before(before, before, "collation-refresh")
        blocked = evaluate()
        blocked["checks"]["collation"] = "BLOCKED"
        blocked["checks"]["backup"] = "BLOCKED"
        with self.assertRaisesRegex(ValueError, "backup"):
            apply_gate.validate_before(blocked, blocked, "collation-refresh")

    def test_apply_bindings_reject_wrong_digest_or_approval(self) -> None:
        manifest = "a" * 64
        adapter = "b" * 64
        approval = f"APPLY:collation-refresh:{manifest}:{adapter}"
        apply_gate.validate_bindings(
            "collation-refresh", manifest, manifest, adapter, adapter, approval
        )
        with self.assertRaisesRegex(ValueError, "manifest digest"):
            apply_gate.validate_bindings(
                "collation-refresh", manifest, "c" * 64, adapter, adapter, approval
            )
        with self.assertRaisesRegex(ValueError, "operator approval"):
            apply_gate.validate_bindings(
                "collation-refresh", manifest, manifest, adapter, adapter, "wrong"
            )


if __name__ == "__main__":
    unittest.main()
