"""Completed proof survives reader fixes; verifier and generation drift do not."""
import copy
import runpy
import unittest
from pathlib import Path

M = runpy.run_path(str(Path(__file__).resolve().parents[2] /
                       "ops/storage-v2/integrity_resume.py"))


class IntegrityResumeTests(unittest.TestCase):
    def fixture(self):
        identity = dict(source_id=17, generation_id=23, generation_seq=2,
                        source_watermark_sha256="a" * 64, item_count=4,
                        active_generation_id=None, verification_manifest_sha256="b" * 64,
                        generation_root_sha256="c" * 64, run_id=8,
                        commit_sha="d" * 40, adapter_profile_id="fixture-v1",
                        run_status="sealed", status="verified")
        checkpoint = {k: identity[k] for k in (
            "source_id", "generation_id", "generation_seq", "commit_sha",
            "source_watermark_sha256", "item_count", "active_generation_id")}
        checkpoint["build"] = {"fixture_sha256": "e" * 64}
        verifier = "storage_v2_verify_lexical_segment_page(bigint,bigint,integer)"
        functions = {verifier: "f" * 64, **{k: "1" * 64 for k in M["QUERY_ONLY_CHANGES"]}}
        install = {"status": "PASS", "binaries": {"mainrag-api": {"sha256": "2" * 64}},
                   "function_identities": functions}
        reader = {"binary_sha256": "2" * 64, "installation_receipt_sha256": "3" * 64}
        verified = {**identity, "checks": {k: "PASS" for k in M["INTEGRITY_CHECKS"]},
                    "lexical_segment_verification": {"generation_id": 23,
                    "occurrence_count": 4, "missing_count": 0, "invalid_count": 0}}
        prior = {"status": "FAIL", "failed_gate": "query_coverage", "checkpoint": checkpoint,
                 "reader_package": reader, "verification": verified}
        observed = {"identity": identity, "function_identities": copy.deepcopy(functions)}
        return [prior, checkpoint, copy.deepcopy(identity), install, "3" * 64,
                copy.deepcopy(reader), observed]

    def test_completed_integrity_is_retained_after_query_failure_and_reader_only_changes(self):
        args = self.fixture()
        for name in M["QUERY_ONLY_CHANGES"]:
            args[-1]["function_identities"][name] = "4" * 64
        args[-1]["function_identities"].update({k: "5" * 64 for k in M["QUERY_ONLY_ADDITIONS"]})
        result = M["validate_reuse"](*args)
        self.assertEqual(result, args[0]["verification"])
        self.assertEqual(args[0]["status"], "FAIL")

    def test_rejects_each_generation_and_producer_identity_change(self):
        for field in M["IDENTITY_FIELDS"] + ("generation_root_sha256", "run_id",
                                             "commit_sha", "run_status", "adapter_profile_id"):
            with self.subTest(field=field):
                args = self.fixture()
                args[-1]["identity"][field] = "different"
                with self.assertRaises(RuntimeError):
                    M["validate_reuse"](*args)
        args = self.fixture()
        args[1] = {**args[1], "source_snapshot_review_sha256": "9" * 64}
        with self.assertRaisesRegex(RuntimeError, "checkpoint identity"):
            M["validate_reuse"](*args)

    def test_compaction_reuse_requires_live_catalog_admission_and_unchanged_verifier(self):
        args = self.fixture()
        functions = args[-1]["function_identities"]
        functions.update({name: "4" * 64 for name in M["POSTING_COMPACTION_ADDITIONS"]})
        args[3]["function_identities"].update({name: "1" * 64 for name in M["POSTING_COMPACTION_CHANGES"]})
        functions.update({name: "5" * 64 for name in M["POSTING_COMPACTION_CHANGES"]})
        with self.assertRaisesRegex(RuntimeError, "compaction admission"):
            M["validate_reuse"](*args)
        args[-1]["posting_compaction_admission"] = {
            "schema_version": "mainrag.storage-v2.posting-compaction-admission.v1",
            "validated_guard_sha256": "4" * 64, "contract_sha256": "6" * 64}
        self.assertEqual(M["validate_reuse"](*args), args[0]["verification"])
        for fault in ("guard", "contract", "schema", "missing_helper", "verifier"):
            with self.subTest(fault=fault):
                changed = copy.deepcopy(args)
                admission = changed[-1]["posting_compaction_admission"]
                if fault == "guard": admission["validated_guard_sha256"] = "7" * 64
                elif fault == "contract": admission["contract_sha256"] = "invalid"
                elif fault == "schema": admission["schema_version"] = "unreviewed"
                elif fault == "missing_helper":
                    del changed[-1]["function_identities"][next(iter(M["POSTING_COMPACTION_ADDITIONS"]))]
                else:
                    changed[-1]["function_identities"]["storage_v2_verify_lexical_segment_page(bigint,bigint,integer)"] = "8" * 64
                with self.assertRaises(RuntimeError):
                    M["validate_reuse"](*changed)

    def test_rejects_verifier_change_removal_addition_and_binary_drift(self):
        for change in ("definition", "removal", "addition", "binary", "receipt"):
            with self.subTest(change=change):
                args = self.fixture()
                functions = args[-1]["function_identities"]
                verifier = next(k for k in functions if k not in M["QUERY_ONLY_CHANGES"])
                if change == "definition": functions[verifier] = "6" * 64
                elif change == "removal": del functions[verifier]
                elif change == "addition": functions["storage_v2_new_verifier()"] = "7" * 64
                elif change == "binary": args[5]["binary_sha256"] = "8" * 64
                else: args[0]["reader_package"]["installation_receipt_sha256"] = "9" * 64
                with self.assertRaises(RuntimeError):
                    M["validate_reuse"](*args)

    def traversal_fixture(self):
        args = self.fixture()
        name = M["LEXICAL_PAGE_SIGNATURE"]
        args[3]["function_identities"][name] = M["LEXICAL_PAGE_PREDECESSOR_SHA256"]
        args[-1]["function_identities"][name] = M["LEXICAL_PAGE_SEEK_SHA256"]
        args[-1]["lexical_traversal_admission"] = {
            "schema_version": "mainrag.storage-v2.lexical-page-traversal-admission.v1",
            "predecessor_sha256": M["LEXICAL_PAGE_PREDECESSOR_SHA256"],
            "current_sha256": M["LEXICAL_PAGE_SEEK_SHA256"],
            "authority_and_multiplicity_validated": True}
        return args

    def test_exact_selection_change_reuses_complete_proof_with_live_admission(self):
        args = self.traversal_fixture()
        self.assertEqual(M["validate_reuse"](*args), args[0]["verification"])
        args[3]["function_identities"][M["LEXICAL_PAGE_SIGNATURE"]] = M["LEXICAL_PAGE_SEEK_SHA256"]
        self.assertEqual(M["validate_reuse"](*args), args[0]["verification"])
        del args[-1]["lexical_traversal_admission"]
        with self.assertRaisesRegex(RuntimeError, "traversal admission"):
            M["validate_reuse"](*args)

    def test_selection_reuse_rejects_unknown_predecessor_or_admission_drift(self):
        for fault in ("missing", "schema_version", "predecessor_sha256", "current_sha256",
                      "authority_and_multiplicity_validated", "unknown_predecessor"):
            with self.subTest(fault=fault):
                args = self.traversal_fixture()
                if fault == "missing":
                    del args[-1]["lexical_traversal_admission"]
                elif fault == "unknown_predecessor":
                    args[3]["function_identities"][M["LEXICAL_PAGE_SIGNATURE"]] = "9" * 64
                else:
                    args[-1]["lexical_traversal_admission"][fault] = "unreviewed"
                with self.assertRaises(RuntimeError):
                    M["validate_reuse"](*args)

    def test_selection_admission_does_not_allow_other_verifier_changes(self):
        args = self.traversal_fixture()
        args[3]["function_identities"]["storage_v2_verify_roots(bigint)"] = "6" * 64
        args[-1]["function_identities"]["storage_v2_verify_roots(bigint)"] = "7" * 64
        with self.assertRaisesRegex(RuntimeError, "verifier definitions"):
            M["validate_reuse"](*args)
        args[-1]["function_identities"]["storage_v2_verify_roots(bigint)"] = "6" * 64
        functions = args[-1]["function_identities"]
        functions.update({name: "4" * 64 for name in M["POSTING_COMPACTION_ADDITIONS"]})
        args[3]["function_identities"].update({name: "1" * 64 for name in M["POSTING_COMPACTION_CHANGES"]})
        functions.update({name: "5" * 64 for name in M["POSTING_COMPACTION_CHANGES"]})
        args[-1]["posting_compaction_admission"] = {
            "schema_version": "mainrag.storage-v2.posting-compaction-admission.v1",
            "validated_guard_sha256": "4" * 64, "contract_sha256": "6" * 64}
        self.assertEqual(M["validate_reuse"](*args), args[0]["verification"])

    def test_incomplete_or_corrupt_proof_is_never_reused(self):
        for check in M["INTEGRITY_CHECKS"]:
            with self.subTest(check=check):
                args = self.fixture()
                args[0]["verification"]["checks"][check] = "FAIL"
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    M["validate_reuse"](*args)
        for field in ("invalid_count", "missing_count", "occurrence_count"):
            with self.subTest(field=field):
                args = self.fixture()
                args[0]["verification"]["lexical_segment_verification"][field] = 99
                with self.assertRaisesRegex(RuntimeError, "coverage is incomplete"):
                    M["validate_reuse"](*args)
