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
        "generation_seq": 1,
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
    def test_current_reader_package_is_required_when_auditing_new_reader_acceptance(self):
        reader = {"commit_sha": "9" * 40, "binary_sha256": "8" * 64}
        regular, benchmark = source(1), source(2, benchmark=True)
        inventory = self.inventory(regular, benchmark)
        protected, _ = AUDIT.audit(inventory, "f" * 64, reader)
        self.assertEqual(protected["persisted_gate_blockers"]["current_reader_package_not_proven"], 2)
        self.assertFalse(protected["persisted_candidate_set_complete"])
        for item in (regular, benchmark):
            item["generations"][0]["qualification_manifest"]["reader_package"] = {
                **reader, "installation_receipt_sha256": "6" * 64, "server_instance_id": "current-instance"}
        protected, _ = AUDIT.audit(inventory, "f" * 64, reader)
        self.assertTrue(protected["persisted_candidate_set_complete"])
        regular["generations"][0]["qualification_manifest"]["reader_package"]["binary_sha256"] = "7" * 64
        protected, _ = AUDIT.audit(inventory, "f" * 64, reader)
        self.assertEqual(protected["persisted_gate_blockers"]["current_reader_package_not_proven"], 1)

    @staticmethod
    def inventory(*sources: dict) -> dict:
        return {"inventory_id": "fixture", "operator_commit_sha": "d" * 40,
                "candidate_commit_sha": "b" * 40,
                "sources": list(sources)}

    def test_cut_manifest_and_complete_file_proof_keep_original_build_and_generation_identity(self) -> None:
        import copy
        import uuid
        reviewed = source(1); candidate = reviewed["generations"][0]
        manifest = candidate["qualification_manifest"]
        original = {"cut":{"format":"mainrag.fs-read-cut.v1", "cut_id":str(uuid.uuid4()),
                    "snapshot_uuid":str(uuid.uuid4()), "origin_uuid":str(uuid.uuid4()),
                    "source_root_sha256":"2"*64, "descriptor_sha256":"4"*64, "captured_at_unix":1},
                    "fixture_sha256":"5"*64,"item_count":2,"input_bytes":8}
        current = copy.deepcopy(original); current["cut"].update(cut_id=str(uuid.uuid4()),
            snapshot_uuid=str(uuid.uuid4()),descriptor_sha256="6"*64,captured_at_unix=2)
        profile="mainrag.fs-release-candidate.v4.btrfs-cut-v1.scope-unfiltered.fragment-1048576-newline-65536"
        candidate.update(adapter_profile_id=profile,filesystem_cut=original)
        manifest["source_snapshot_review"]={"review_sha256":"1"*64,"source_watermark_sha256":"c"*64,
            "source_root_sha256":"2"*64,"adapter_profile_id":profile,"item_count":2,
            "filesystem_cut":current,"build_filesystem_cut":original,"status_counts":{"changed_bytes":1}}
        manifest["gold_suite_summary"].update(source_snapshot_review_sha256="1"*64,
            source_snapshot_gold_review_sha256="3"*64)
        for query in manifest["query_results"]:
            query.update(expected_in_storage_v2=True)
            query["coverage"]={"passed":True,"policy":"simple-conjunction-source-snapshot-v1",
                "source_snapshot_review_sha256":"1"*64,"baseline_paths_retained_in_order":True,
                "all_candidate_hits_supported":True,"all_current_hits_supported":True,
                "same_byte_baseline_path_count":0,"stale_baseline_path_count":1}
        complete={"schema_version":"mainrag.storage-v2.complete-source-file.v1",
            "source_id":1,"generation_id":candidate["generation_id"],"generation_seq":candidate["generation_seq"],
            "commit_sha":candidate["commit_sha"],"path_sha256":"7"*64,"body_sha256":"8"*64,
            "item_manifest_sha256":"9"*64,"fragment_count":2,"logical_bytes":8,"byte_start":0,"byte_end":8,
            "all_fragments_verified":True}
        manifest["query_results"][0]["coverage"]["expected_source_body"]={
            "schema_version":"mainrag.storage-v2.expected-source-body.v2","path_sha256":"7"*64,
            "observed_sha256":"8"*64,"legacy_sha256":"a"*64,"body_sha256":"8"*64,
            "source_status":"changed_bytes","query_body_match":True,
            "source_snapshot_review_sha256":"1"*64,"complete_source_file":complete}
        inventory=self.inventory(reviewed,source(2,benchmark=True))
        result,_=AUDIT.audit(inventory,"e"*64);self.assertTrue(result["persisted_candidate_set_complete"])
        candidate["adapter_profile_id"] = profile + ".text-utf8-nul-space-v1"
        manifest["source_snapshot_review"]["adapter_profile_id"] = candidate["adapter_profile_id"]
        projected,_ = AUDIT.audit(inventory,"e"*64)
        self.assertTrue(projected["persisted_candidate_set_complete"])
        manifest["source_snapshot_review"]["adapter_profile_id"] = profile + ".text-unknown"
        rejected,_ = AUDIT.audit(inventory,"e"*64)
        self.assertIn("source_snapshot_cut_invalid", rejected["persisted_gate_blockers"])
        candidate["adapter_profile_id"] = profile
        manifest["source_snapshot_review"]["adapter_profile_id"] = profile
        current["input_bytes"]=9
        rejected,_=AUDIT.audit(inventory,"e"*64)
        self.assertIn("source_snapshot_cut_invalid",rejected["persisted_gate_blockers"])
        current["input_bytes"]=8;complete["generation_id"]+=1
        rejected,_=AUDIT.audit(inventory,"e"*64)
        self.assertIn("source_file_proof_generation_mismatch",rejected["persisted_gate_blockers"])
        complete["generation_id"]-=1;complete["byte_end"]=7
        rejected,_=AUDIT.audit(inventory,"e"*64)
        self.assertIn("expected_source_file_proof_invalid",rejected["persisted_gate_blockers"])

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
        self.assertEqual(protected["candidate_set"][0]["candidate_commit_sha"], "b" * 40)
        self.assertEqual(public["validated_query_count"], 4)
        self.assertEqual(public["candidate_set_sha256"],
                         AUDIT.hashlib.sha256(AUDIT.canonical(protected["candidate_set"])).hexdigest())
        serialized = json.dumps(public)
        for private in ("private-source-name", "/private/source-path", '"source_id"'):
            self.assertNotIn(private, serialized)

    def test_source_snapshot_review_is_checked_by_the_aggregate(self) -> None:
        reviewed = source(1)
        candidate = reviewed["generations"][0]
        manifest = candidate["qualification_manifest"]
        manifest["source_snapshot_review"] = {
            "review_sha256": "1" * 64,
            "source_watermark_sha256": candidate["source_watermark_sha256"],
            "status_counts": {"same_bytes": 1, "changed_bytes": 1},
        }
        manifest["gold_suite_summary"]["source_snapshot_review_sha256"] = "1" * 64
        manifest["gold_suite_summary"]["source_snapshot_gold_review_sha256"] = "3" * 64
        for query in manifest["query_results"]:
            query["coverage"] = {
                "passed": True, "policy": "simple-conjunction-source-snapshot-v1",
                "source_snapshot_review_sha256": "1" * 64,
                "baseline_paths_retained_in_order": True,
                "all_candidate_hits_supported": True,
                "all_current_hits_supported": True,
                "same_byte_baseline_path_count": 1,
                "stale_baseline_path_count": 1,
            }
        inventory = self.inventory(reviewed, source(2, benchmark=True))
        good, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertTrue(good["persisted_candidate_set_complete"])
        snapshot = manifest["source_snapshot_review"]
        snapshot["status_counts"]["outside_configured_scope"] = 1
        invalid_scope, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(invalid_scope["persisted_gate_blockers"], {"source_snapshot_scope_invalid": 1})
        snapshot.update(filesystem_scope_sha256="6" * 64, adapter_profile_id=
            "mainrag.fs-release-candidate.v3.scope-" + "6" * 64 + ".fragment-1048576-newline-65536")
        mismatched_scope, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(mismatched_scope["persisted_gate_blockers"], {"source_snapshot_scope_profile_mismatch": 1})
        candidate["adapter_profile_id"] = snapshot["adapter_profile_id"]
        valid_scope, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertTrue(valid_scope["persisted_candidate_set_complete"])
        query = manifest["query_results"][0]
        query.update(expected_in_current=True, expected_in_storage_v2=True)
        query["automatic_expectation_binding"] = {
            "policy": "unchanged-legacy-positive-before-candidate-read-v1",
            "original_expected_path_sha256": "4" * 64,
            "expected_path_sha256": "5" * 64,
            "original_source_status": "changed_bytes",
            "source_snapshot_review_sha256": "1" * 64,
        }
        rebound, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertTrue(rebound["persisted_candidate_set_complete"])
        query["automatic_expectation_binding"]["source_snapshot_review_sha256"] = "2" * 64
        unbound, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(unbound["persisted_gate_blockers"], {"automatic_expectation_binding_invalid": 1})
        del query["automatic_expectation_binding"]
        body = {
            "schema_version": "mainrag.storage-v2.expected-source-body.v1",
            "path_sha256": "4" * 64, "observed_sha256": "7" * 64,
            "legacy_sha256": "8" * 64, "body_sha256": "7" * 64,
            "source_status": "changed_bytes", "query_body_match": True,
            "source_snapshot_review_sha256": "1" * 64,
        }
        query["coverage"]["expected_source_body"] = body
        query["coverage"]["same_byte_baseline_path_count"] = 0
        exact_source, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertTrue(exact_source["persisted_candidate_set_complete"])
        for field, wrong in (("observed_sha256", "9" * 64), ("body_sha256", "9" * 64),
                             ("legacy_sha256", "7" * 64), ("query_body_match", False),
                             ("source_snapshot_review_sha256", "2" * 64),
                             ("source_status", "source_file_missing")):
            old = body[field]
            body[field] = wrong
            rejected, _ = AUDIT.audit(inventory, "e" * 64)
            self.assertEqual(rejected["persisted_gate_blockers"], {"expected_source_body_proof_invalid": 1})
            body[field] = old
        del query["coverage"]["expected_source_body"]
        query["coverage"]["same_byte_baseline_path_count"] = 1
        manifest["query_results"][0]["coverage"]["source_snapshot_review_sha256"] = "2" * 64
        bad, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(bad["persisted_gate_blockers"],
                         {"source_snapshot_query_contract_invalid": 1})

    def test_final_mixed_commit_map_binds_source_local_candidate_commits(self) -> None:
        first = source(1)
        second = source(2, benchmark=True)
        second["generations"][0]["commit_sha"] = "c" * 40
        inventory = self.inventory(first, second)
        inventory.update(
            schema_version="mainrag.storage-v2.candidate-inventory.v2",
            candidate_commit_sha=None,
            candidate_commit_map_sha256="a" * 64,
            candidate_commit_map={
                "schema_version": "mainrag.storage-v2.final-candidate-commit-map.v1",
                "sources": [
                    {"source_id": 1, "candidate_generation_id": 101,
                     "candidate_commit_sha": "b" * 40},
                    {"source_id": 2, "candidate_generation_id": 102,
                     "candidate_commit_sha": "c" * 40},
                ],
            },
        )
        protected, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertTrue(protected["persisted_candidate_set_complete"])
        self.assertEqual([item["candidate_commit_sha"] for item in protected["candidate_set"]],
                         ["b" * 40, "c" * 40])
        inventory["candidate_commit_map"]["sources"][1]["candidate_commit_sha"] = "f" * 40
        blocked, _ = AUDIT.audit(inventory, "e" * 64)
        self.assertEqual(blocked["persisted_gate_blockers"],
                         {"candidate_package_identity_mismatch": 1})

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
