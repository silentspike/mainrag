"""Fail-closed planning and committed-readback tests for issue 67."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/activation-set.py"
SPEC = importlib.util.spec_from_file_location("activation_set_operator", PATH)
assert SPEC and SPEC.loader
OPERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(OPERATOR)


def fixture(binary: Path, now: int) -> tuple[dict, str, dict, str, dict, str, list[dict]]:
    candidate = {
        "source_id": 1, "candidate_generation_id": 7,
        "expected_active_generation_id": None,
        "evidence_id": "00000000-0000-4000-8000-000000000001",
        "evidence_manifest_sha256": "1" * 64,
        "source_watermark_sha256": "2" * 64,
    }
    stored = {**candidate, "adapter_profile_id": "fixture-adapter",
              "candidate_commit_sha": "e" * 40,
              "item_count": 1,
              "analysis_profile_id": "fixture-analysis",
              "search_profile_id": "fixture-search",
              "verification_manifest_sha256": "3" * 64,
              "gold_suite_sha256": "4" * 64}
    candidate_set = [stored]
    audit = {
        "schema_version": "mainrag.storage-v2.candidate-aggregate-audit.v2",
        "persisted_candidate_set_complete": True,
        "candidate_set_sha256": OPERATOR.sha256(OPERATOR.canonical(candidate_set)),
        "candidate_set": candidate_set,
    }
    audit_sha = "5" * 64
    acceptance = {
        "schema_version": "mainrag.storage-v2.aggregate-acceptance.v1",
        "status": "PASS", "persisted_audit_sha256": audit_sha,
        "candidate_set_sha256": audit["candidate_set_sha256"],
        "source_count": 1, "code_commit_sha": "c" * 40,
        "preflight_operator_commit_sha": "d" * 40,
        "schema_sha256": "a" * 64,
        "backend_package_sha256": "b" * 64,
        "installed_binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "current_source_watermarks": [{"source_id": 1,
                                      "watermark_sha256": "2" * 64,
                                      "observed_at_unix": now}],
        "external_gates": {name: {"status": "PASS", "evidence_sha256": "e" * 64,
                                  "observed_at_unix": now}
                           for name in OPERATOR.EXTERNAL_GATES},
    }
    preflight = {
        "mode": "check", "overall_status": "PASS",
        "checks": {"capacity": "PASS", "backup": "PASS", "maintenance": "PASS"},
        "generated_at_unix": now,
        "candidate": {"commit_sha": "d" * 40, "schema_sha256": "a" * 64},
    }
    live = [{"source_id": 1, "is_test": True, "active_generation_id": None,
             "candidates": [{"generation_id": 7, "status": "release_candidate",
                             "evidence_id": candidate["evidence_id"],
                             "evidence_manifest_sha256": "1" * 64,
                             "source_watermark_sha256": "2" * 64,
                             "commit_sha": "e" * 40}]}]
    return audit, audit_sha, acceptance, "6" * 64, preflight, "7" * 64, live


class ActivationOperatorTests(unittest.TestCase):
    def test_complete_plan_binds_distinct_operator_and_runtime_commits(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "api"
            binary.write_bytes(b"synthetic-package")
            audit, audit_sha, acceptance, accepted_sha, preflight, preflight_sha, live = fixture(binary, now)
            with patch.object(OPERATOR, "manifest_digest", return_value="8" * 64):
                plan = OPERATOR.make_plan(audit, audit_sha, acceptance, accepted_sha,
                                          preflight, preflight_sha, binary, live,
                                          "fixture", False, now)
            self.assertEqual(plan["status"], "READY_FOR_EXPLICIT_APPROVAL")
            self.assertEqual(plan["manifest"]["code_commit_sha"], "c" * 40)
            self.assertEqual(plan["manifest"]["aggregate_evidence_sha256"], accepted_sha)
            self.assertEqual(plan["manifest"]["sources"][0]["expected_active_generation_id"], None)
            self.assertEqual(len(plan["manifest"]["sources"][0]), 7)
            self.assertEqual(plan["manifest_sha256"], "8" * 64)
            self.assertEqual(plan["default_switch"]["unit"], "mainrag-api.service")

    def test_stale_watermark_package_preflight_and_live_candidate_fail_closed(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "api"
            binary.write_bytes(b"synthetic-package")
            audit, audit_sha, acceptance, accepted_sha, preflight, preflight_sha, live = fixture(binary, now)
            cases = (
                (lambda: acceptance["current_source_watermarks"][0].update(
                    observed_at_unix=now - 301), "watermark identity"),
                (lambda: acceptance.update(installed_binary_sha256="0" * 64), "installed binary"),
                (lambda: preflight["checks"].update(capacity="BLOCKED"), "preflight"),
                (lambda: live[0]["candidates"][0].update(commit_sha="f" * 40), "qualification"),
            )
            for change, expected in cases:
                audit, audit_sha, acceptance, accepted_sha, preflight, preflight_sha, live = fixture(binary, now)
                change()
                with self.subTest(expected=expected), \
                     self.assertRaisesRegex(RuntimeError, expected), \
                     patch.object(OPERATOR, "manifest_digest") as digest:
                    OPERATOR.make_plan(audit, audit_sha, acceptance, accepted_sha,
                                       preflight, preflight_sha, binary, live,
                                       "fixture", False, now)
                digest.assert_not_called()

    def test_approval_and_commit_readback_reject_mixed_state(self) -> None:
        now = int(time.time())
        manifest = {"activation_id": "00000000-0000-4000-8000-000000000002",
                    "code_commit_sha": "c" * 40, "schema_sha256": "a" * 64,
                    "backend_package_sha256": "b" * 64,
                    "sources": [{"source_id": 1, "candidate_generation_id": 7,
                                 "expected_active_generation_id": None}]}
        plan = {"manifest": manifest, "manifest_sha256": "1" * 64,
                "candidate_set_sha256": "2" * 64,
                "before_pointer_set_sha256": "3" * 64}
        approval = {"schema_version": "mainrag.storage-v2.activation-approval.v1",
                    "status": "APPROVED", "plan_sha256": "4" * 64,
                    "manifest_sha256": "1" * 64, "code_commit_sha": "c" * 40,
                    "schema_sha256": "a" * 64,
                    "backend_package_sha256": "b" * 64,
                    "candidate_set_sha256": "2" * 64,
                    "before_pointer_set_sha256": "3" * 64,
                    "approved_at_unix": now}
        OPERATOR.validate_approval(approval, plan, "4" * 64, now)
        with self.assertRaisesRegex(RuntimeError, "fresh exact"):
            OPERATOR.validate_approval({**approval, "approved_at_unix": now - 901},
                                       plan, "4" * 64, now)
        readback = {"receipt": {"id": manifest["activation_id"],
                                "manifest_sha256": "1" * 64,
                                "source_count": 1,
                                "pointer_set_sha256": "5" * 64},
                    "pointer_set_sha256": "5" * 64,
                    "sources": [{"source_id": 1, "active_generation_id": 7,
                                 "active_status": "active", "active_generation_count": 1}]}
        OPERATOR.verify_committed(plan, readback)
        with self.assertRaisesRegex(RuntimeError, "pointer set differs"):
            OPERATOR.verify_committed(plan, {**readback, "sources": [
                {**readback["sources"][0], "active_generation_count": 2}]})
        previous = {**plan, "manifest": {**manifest, "sources": [
            {**manifest["sources"][0], "expected_active_generation_id": 3}]}}
        with self.assertRaisesRegex(RuntimeError, "not superseded"):
            OPERATOR.verify_committed(previous, readback)
        OPERATOR.verify_committed(previous, {**readback, "sources": [
            {**readback["sources"][0], "generation_statuses": [
                {"generation_id": 3, "status": "superseded"}]}]})
        sql = OPERATOR.activation_sql(manifest, "1" * 64,
                                      "00000000-0000-4000-8000-000000000003")
        self.assertIn("storage_v2_activate_candidate_set", sql)
        self.assertIn("COMMIT;", sql)
        self.assertNotIn("UPDATE source_generation", sql)

    def test_protected_evidence_is_private_create_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            OPERATOR.private_write(path, {"status": "first"})
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(OPERATOR.read_private(path, digest)["status"], "first")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                OPERATOR.private_write(path, {"status": "second"})
            self.assertEqual(OPERATOR.read_private(path, digest)["status"], "first")

    def test_live_watermark_gate_rejects_stale_source(self) -> None:
        expected = [{"source_id": 1, "source_watermark_sha256": "a" * 64,
                     "adapter_profile_id": "fixture", "item_count": 2}]
        observed = {"source_id": 1, "source_watermark_sha256": "a" * 64,
                    "adapter_profile_id": "fixture", "item_count": 2}
        class Opener:
            def open(self, request, timeout):
                self.request = request
                return io.BytesIO(json.dumps(observed).encode())
        opener = Opener()
        with patch.object(OPERATOR.urllib.request, "build_opener", return_value=opener):
            OPERATOR.verify_current_api_watermarks("http://127.0.0.1:3001", "private", expected)
            self.assertIn("Bearer private", opener.request.get_header("Authorization"))
            observed["source_watermark_sha256"] = "b" * 64
            with self.assertRaisesRegex(RuntimeError, "watermark drifted"):
                OPERATOR.verify_current_api_watermarks(
                    "http://127.0.0.1:3001", "private", expected)


if __name__ == "__main__":
    unittest.main()
