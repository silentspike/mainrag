"""Fail-closed, public-safe checks for the candidate-set audit."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path


PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/candidate-aggregate-audit.py"
SPEC = importlib.util.spec_from_file_location("candidate_aggregate_audit", PATH)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def source(source_id: int, *, benchmark: bool = False, candidate: bool = True,
           gold: bool = True) -> dict:
    manifest = {"status": "PASS", "checks": {name: "PASS" for name in AUDIT.CHECKS}}
    manifest.update({
        "query_seed_summary": {"case_count": 0},
        "query_results": [{"id": f"fixture-{index}", "quality_passed": True,
                           "performance_passed": True, "degradation_passed": True,
                           "storage_v2_took_ms": 1, "max_query_ms": 2000,
                           "coverage": None} for index in range(2)],
        "server_verification_sha256": "e" * 64,
        "dual_read_artifact_sha256": "f" * 64,
        "dual_read_evidence_id": "00000000-0000-4000-8000-000000000001",
        "query_coverage_sha256": "0" * 64,
        "resource": {"free_bytes": 100, "minimum_free_bytes": 50},
        "restart": {"server_instance_changed": True, "generation_reused": True},
        "intelligence": {"applicability": "unknown_not_applicable", "commands": []},
    })
    if gold:
        manifest["gold_suite_summary"] = {
            "schema_version": "mainrag.storage-v2.gold-suite-summary.v1",
            "suite_sha256": "a" * 64,
            "source_class": "fixture-class",
            "case_count": 2,
            "positive_case_count": 1,
            "negative_case_count": 1,
            "distinct_query_count": 2,
            "representative_coverage": "SUITE_DIGEST_BOUND_REVIEW_EXTERNAL",
        }
    generation = {
        "generation_id": source_id + 100,
        "status": "release_candidate",
        "evidence_id": "fixture-evidence",
        "commit_sha": "b" * 40,
        "source_watermark_sha256": "c" * 64,
        "adapter_profile_id": "fixture-adapter",
        "analysis_profile_id": "fixture-analysis",
        "search_profile_id": "fixture-search",
        "verification_manifest_sha256": "e" * 64,
        "item_count": 2,
        "qualification_manifest": manifest,
        "qualification_manifest_sha256": "d" * 64,
        "qualification_manifest_digest_matches": True,
    }
    return {
        "source_id": source_id,
        "source_ref": f"{source_id:064x}",
        "name": "private-source-name",
        "path": "/private/source-path",
        "is_test": benchmark,
        "active_generation_id": None,
        "generations": [generation] if candidate else [],
    }


class CandidateAggregateAuditTests(unittest.TestCase):
    @staticmethod
    def inventory(*sources: dict) -> dict:
        return {"inventory_id": "fixture", "operator_commit_sha": "b" * 40,
                "sources": list(sources)}

    def test_complete_persisted_shape_remains_observed_only(self) -> None:
        inventory = self.inventory(source(1), source(2, benchmark=True))
        protected, public = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(public["persisted_gate_blockers"], {})
        self.assertEqual(public["release_candidate_source_count"], 2)
        self.assertEqual(public["benchmark_candidate_count"], 1)
        self.assertEqual(public["status"], "BLOCKED")
        self.assertEqual(public["external_gate_count"], len(AUDIT.EXTERNAL_GATES))
        self.assertEqual(protected["sources"][0]["blockers"], [])
        self.assertTrue(protected["persisted_candidate_set_complete"])
        self.assertEqual(len(protected["candidate_set"]), 2)
        self.assertEqual(public["validated_query_count"], 4)
        self.assertEqual(public["candidate_set_sha256"],
                         AUDIT.hashlib.sha256(AUDIT.canonical(protected["candidate_set"])).hexdigest())
        serialized = json.dumps(public)
        for private in ("private-source-name", "/private/source-path", '"source_id"'):
            self.assertNotIn(private, serialized)

    def test_missing_candidate_and_gold_binding_are_counted_without_acceptance(self) -> None:
        inventory = self.inventory(source(1, gold=False), source(2, benchmark=True, candidate=False))
        protected, public = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(public["persisted_gate_blockers"]["gold_suite_binding_missing"], 1)
        self.assertEqual(public["persisted_gate_blockers"]["missing_release_candidate"], 1)
        self.assertEqual(public["benchmark_candidate_count"], 0)
        self.assertIn("gold_suite_binding_missing", protected["sources"][0]["blockers"])
        self.assertEqual(protected["sources"][1]["blockers"], ["missing_release_candidate"])
        self.assertFalse(protected["persisted_candidate_set_complete"])
        self.assertEqual(protected["candidate_set"], [])

    def test_persisted_manifest_mismatch_is_a_blocker(self) -> None:
        broken = source(1)
        broken["generations"][0]["qualification_manifest_digest_matches"] = False
        protected, public = AUDIT.audit(
            self.inventory(broken, source(2, benchmark=True)),
            "e" * 64)
        self.assertEqual(public["persisted_gate_blockers"], {
            "qualification_manifest_digest_mismatch": 1})
        self.assertIn("qualification_manifest_digest_mismatch",
                      protected["sources"][0]["blockers"])

    def test_false_pass_label_does_not_hide_query_or_package_failure(self) -> None:
        broken = source(1)
        candidate = broken["generations"][0]
        candidate["commit_sha"] = "a" * 40
        candidate["qualification_manifest"]["query_results"][0]["quality_passed"] = False
        protected, public = AUDIT.audit(
            self.inventory(broken, source(2, benchmark=True)), "e" * 64)
        self.assertEqual(public["persisted_gate_blockers"]["query_result_gate_failed"], 1)
        self.assertEqual(public["persisted_gate_blockers"]["candidate_package_identity_mismatch"], 1)
        self.assertFalse(protected["persisted_candidate_set_complete"])

    def test_missing_profile_and_unproven_resource_block_candidate_set(self) -> None:
        broken = source(1)
        candidate = broken["generations"][0]
        candidate["adapter_profile_id"] = None
        candidate["qualification_manifest"]["resource"]["free_bytes"] = 1
        protected, public = AUDIT.audit(
            self.inventory(broken, source(2, benchmark=True)), "e" * 64)
        self.assertEqual(public["persisted_gate_blockers"]["candidate_identity_incomplete"], 1)
        self.assertEqual(public["persisted_gate_blockers"]["resource_receipt_invalid"], 1)
        self.assertIsNone(protected["candidate_set_sha256"])

    def test_invalid_inventory_and_digest_fail_closed(self) -> None:
        inventory = {"schema_version": "mainrag.storage-v2.candidate-inventory.v1",
                     "capture_status": "OBSERVED_ONLY", "sources": [source(1)]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inventory.json"
            AUDIT.private_create(path, inventory)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(AUDIT.read_protected_inventory(path, digest)[1], digest)
            with self.assertRaisesRegex(RuntimeError, "digest differs"):
                AUDIT.read_protected_inventory(path, "0" * 64)
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "private regular file"):
                AUDIT.read_protected_inventory(path, digest)
        with self.assertRaisesRegex(RuntimeError, "source identity"):
            AUDIT.audit({"sources": [source(1), source(1)]}, "e" * 64)

    def test_output_is_create_only_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "audit.json"
            AUDIT.private_create(path, {"private": "first"})
            first = path.read_bytes()
            with self.assertRaises(FileExistsError):
                AUDIT.private_create(path, {"private": "second"})
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
