"""Fail-closed planning and committed-readback tests for issue 67."""

from __future__ import annotations

import hashlib
import copy
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


class PostIngestReadbackTests(unittest.TestCase):
    def fixture(self):
        plan_sha = "a" * 64
        manifest_sha = "b" * 64
        activation_id = "00000000-0000-4000-8000-000000000002"
        plan = {"schema_version": "mainrag.storage-v2.activation-plan.v1",
                "status": "READY_FOR_EXPLICIT_APPROVAL",
                "manifest_sha256": manifest_sha, "manifest": {
            "activation_id": activation_id, "code_commit_sha": "c" * 40,
            "sources": [{"source_id": 7, "candidate_generation_id": 41},
                        {"source_id": 8, "candidate_generation_id": 91}]}}
        joined = {"activation_id": activation_id, "manifest_sha256": manifest_sha,
                  "plan_sha256": plan_sha}
        attempt = {**joined, "schema_version": "mainrag.storage-v2.activation-attempt.v1",
                   "status": "DB_COMMITTED_DEFAULT_SWITCH_PENDING", "pointer_set_sha256": "e" * 64}
        switched = {**joined, "schema_version": "mainrag.storage-v2.default-switch.v1",
                    "status": "DEFAULT_SWITCHED_POST_INGEST_PENDING", "observed_at_unix": 100}
        api_receipt = {"status": "ACTIVE_INGEST_COMMITTED", "receipt_id": 11,
                       "source_id": 7, "generation_id": 42, "pointer_set_sha256": "d" * 64}
        native = {"status": "ACTIVE_INGEST_COMMITTED", "sync_mode": "full_source_observation",
                  "generation_id": 42, "generation_seq": 3, "item_count": 12,
                  "changed_item_count": 2, "receipt": api_receipt,
                  "telemetry": {"generation_reused": False}}
        response = {"status": "completed", "source_id": 7, "error_details": [],
                    "stats": {"errors": 0, "source_io": {"application_read_bytes": None}},
                    "storage_v2": native}
        receipt = {"id": 11, "activation_id": activation_id, "source_id": 7,
                   "prior_generation_id": 41, "generation_id": 42, "run_id": 19,
                   "before_pointer_set_sha256": "e" * 64, "after_pointer_set_sha256": "d" * 64,
                   "source_count": 2, "source_classification_sha256": "f" * 64,
                   "source_watermark_sha256": "1" * 64, "verification_manifest_sha256": "2" * 64,
                   "verification_proof_sha256": "3" * 64, "created_at_unix": 101.5}
        readback = {"receipt": receipt, "receipt_count": 1,
                    "pointer_set_sha256": "d" * 64, "source_classification_sha256": "f" * 64,
                    "activation": {"id": activation_id, "manifest_sha256": manifest_sha,
                        "source_count": 2, "pointer_set_sha256": "e" * 64,
                        "source_classification_sha256": "f" * 64},
                    "sources": [{"source_id": 7, "is_test": False, "active_generation_id": 42,
                        "active_status": "active", "active_generation_count": 1, "generation_seq": 3,
                        "item_count": 12, "changed_item_count": 2, "prior_status": "superseded",
                        "verification_manifest_sha256": "2" * 64,
                        "witness": {"source_watermark_sha256": "1" * 64,
                                    "commit_sha": "c" * 40, "adapter_profile_id": "fixture-v1"}},
                        {"source_id": 8, "active_generation_id": 91,
                         "active_status": "active", "active_generation_count": 1}],
                    "run": {"id": 19, "source_id": 7, "generation_id": 42, "status": "sealed",
                        "expected_active_generation_id": 41, "semantic_manifest_sha256": "1" * 64,
                        "expected_item_count": 12, "adapter_profile_id": "fixture-v1"}}
        return plan, attempt, switched, response, readback, plan_sha

    def check(self, values):
        return OPERATOR.verify_post_ingest(*values[:5], plan_sha256=values[5])

    def test_actual_api_and_cli_receipts_prove_only_the_ordinary_commit(self):
        values = self.fixture()
        result = self.check(values)
        self.assertTrue(result["regular_ingest_committed"])
        self.assertFalse(result["whole_issue67_accepted"])
        self.assertEqual(result["status"], "POST_INGEST_COMMITTED_ACTIVE_CHECKS_PENDING")
        self.assertIn("benchmark", result["remaining_checks"])
        self.assertIsNone(result["source_io"]["application_read_bytes"])
        flat = {**values[3]["storage_v2"], "source_id": 7,
                "source_io": values[3]["stats"]["source_io"]}
        self.assertEqual(self.check((*values[:3], flat, *values[4:])), result)

    def test_unknown_no_change_and_mixed_pointer_or_receipt_drift_never_pass(self):
        changes = [
            (1, ("status",), "COMMIT_OUTCOME_UNKNOWN"),
            (2, ("status",), "SWITCH_OUTCOME_UNKNOWN"),
            (2, ("plan_sha256",), "0" * 64),
            (3, ("storage_v2", "status"), "NO_CHANGE"),
            (3, ("storage_v2", "changed_item_count"), 0),
            (3, ("storage_v2", "receipt", "receipt_id"), 12),
            (4, ("receipt_count",), 2),
            (4, ("receipt", "before_pointer_set_sha256"), "0" * 64),
            (4, ("receipt", "after_pointer_set_sha256"), "0" * 64),
            (4, ("receipt", "created_at_unix"), 99),
            (4, ("sources", 1, "active_generation_id"), 92),
            (4, ("sources", 0, "is_test"), True),
            (4, ("sources", 0, "changed_item_count"), 3),
            (4, ("sources", 0, "prior_status"), "active"),
            (4, ("sources", 0, "witness", "commit_sha"), "0" * 40),
            (4, ("run", "status"), "building"),
            (4, ("run", "expected_active_generation_id"), 40),
            (4, ("source_classification_sha256",), "0" * 64),
        ]
        for index, path, replacement in changes:
            with self.subTest(path=path):
                values = copy.deepcopy(self.fixture())
                target = values[index]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = replacement
                with self.assertRaises(RuntimeError):
                    self.check(values)

    def test_readback_is_one_readonly_transaction_and_never_resyncs(self):
        plan, _, _, _, snapshot, _ = self.fixture()
        with patch.object(OPERATOR, "psql", return_value=json.dumps(snapshot)) as database:
            self.assertEqual(OPERATOR.post_ingest_readback("fixture", False,
                plan["manifest"]["activation_id"], plan["manifest_sha256"], 11), snapshot)
        self.assertEqual(database.call_count, 1)
        self.assertIs(database.call_args.kwargs["readonly"], True)
        sql = database.call_args.args[1]
        self.assertIn("REPEATABLE READ READ ONLY", sql)
        self.assertIn("storage_v2_require_complete_active_set", sql)
        self.assertNotIn("storage_v2_activate_regular_ingest", sql)
        self.assertNotIn("UPDATE ", sql)
        with self.assertRaises(RuntimeError), patch.object(OPERATOR, "psql") as unused:
            OPERATOR.post_ingest_readback("fixture", False,
                plan["manifest"]["activation_id"], plan["manifest_sha256"], True)
        unused.assert_not_called()


if __name__ == "__main__":
    unittest.main()
