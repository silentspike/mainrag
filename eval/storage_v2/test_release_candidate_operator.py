"""Focused unit checks for the protected release-candidate operator."""

from __future__ import annotations

import importlib.util
import copy
import json
import stat
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "ops" / "storage-v2" / "release-candidate.py"
SPEC = importlib.util.spec_from_file_location("release_candidate_operator", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseCandidateOperatorTests(unittest.TestCase):
    def test_query_seed_summary_counts_repeated_queries_not_independent_cases(self) -> None:
        seeds = [{"query": "private-query", "expects_match": True,
                  "expected_path_sha256": str(index)} for index in range(5)]
        original = copy.deepcopy(seeds)
        summary = MODULE.query_seed_summary(seeds)
        self.assertEqual(summary["case_count"], 5)
        self.assertEqual(summary["distinct_query_count"], 1)
        self.assertEqual(summary["repeated_query_case_count"], 4)
        self.assertEqual(summary["largest_query_group"], 5)
        self.assertEqual(summary["positive_case_count"], 5)
        self.assertEqual(summary["negative_case_count"], 0)
        self.assertEqual(summary["representative_gold_coverage"], "NOT_ESTABLISHED")
        self.assertNotIn("private-query", json.dumps(summary))
        self.assertNotIn("expected_path_sha256", json.dumps(summary))
        self.assertEqual(seeds, original)

    def test_query_seed_summary_preserves_exact_query_and_empty_suite_semantics(self) -> None:
        seeds = [{"query": query, "expects_match": positive}
                 for query, positive in (("Term", True), ("term", True), ("term ", False))]
        summary = MODULE.query_seed_summary(seeds)
        self.assertEqual(summary["distinct_query_count"], 3)
        self.assertEqual(summary["repeated_query_case_count"], 0)
        self.assertEqual(summary["positive_case_count"], 2)
        self.assertEqual(summary["negative_case_count"], 1)
        empty = MODULE.query_seed_summary([])
        for field in ("case_count", "distinct_query_count", "repeated_query_case_count",
                      "largest_query_group", "positive_case_count", "negative_case_count"):
            self.assertEqual(empty[field], 0)
        self.assertEqual(empty["representative_gold_coverage"], "NOT_ESTABLISHED")

    def search_fixture(self):
        hit = {"chunk_id": 1, "file_path": "fixture.txt", "score": 1.0,
               "degradation": {stage: "unavailable" for stage in ("graph", "semantic", "rerank")}}
        seed = {"id": "fixture-one", "query": "private-query", "expects_match": True,
                "expected_path_sha256": MODULE.sha256_text("fixture.txt")}
        return seed, {"results": [hit], "took_ms": 1}

    def test_difference_separates_missing_expected_from_missing_baseline(self):
        seed, current = self.search_fixture()
        candidate = {**current, "results": [{**current["results"][0], "file_path": "other.txt"}]}
        for baseline, storage, location in ((current, current, "both"),
                                            (current, candidate, "current_only"),
                                            (candidate, current, "storage_v2_only"),
                                            (candidate, candidate, "neither")):
            with self.subTest(location=location):
                result = MODULE.search_query_gates(seed, baseline, storage, 2000)
                self.assertEqual(result["diagnostics"]["expected_location"], location)
                self.assertEqual(result["quality_passed"], location == "both")
                self.assertEqual(result["diagnostics"]["corpus_presence"], "NOT_ESTABLISHED")
                self.assertEqual(result["diagnostics"]["ranking_cause"], "NOT_ESTABLISHED")

    def test_difference_separates_reordering_displacement_and_duplicate_paths(self):
        seed, current = self.search_fixture()
        first = current["results"][0]
        second = {**first, "chunk_id": 2, "file_path": "second.txt"}
        third = {**first, "chunk_id": 3, "file_path": "third.txt"}
        duplicate = {**first, "chunk_id": 4}
        baseline = {**current, "results": [first, duplicate, second]}
        reordered = {**current, "results": [second, third, first]}
        snapshot = copy.deepcopy((seed, baseline, reordered))
        diagnostic = MODULE.query_difference_diagnostics(seed, baseline, reordered)
        self.assertEqual(diagnostic["baseline_paths_missing"], 0)
        self.assertEqual(diagnostic["candidate_paths_added"], 1)
        self.assertEqual(diagnostic["current_repeated_path_hits"], 1)
        self.assertEqual(diagnostic["storage_v2_repeated_path_hits"], 0)
        self.assertEqual(diagnostic["observations"], ["retained_baseline_order_changed"])
        self.assertEqual((seed, baseline, reordered), snapshot)
        displaced = MODULE.query_difference_diagnostics(seed, baseline, {**current, "results": [first, third]})
        self.assertTrue(displaced["common_path_order_equal"])
        self.assertEqual(displaced["observations"], ["baseline_paths_missing_from_top_k"])

    def test_difference_does_not_publish_source_text_or_exempt_negative_cases(self):
        seed, current = self.search_fixture()
        negative = {**seed, "expects_match": False}
        empty = {"results": [], "took_ms": 1}
        diagnostic = MODULE.query_difference_diagnostics(negative, empty, current)
        self.assertEqual(diagnostic["expected_location"], "not_applicable")
        self.assertIn("unexpected_negative_case_hits", diagnostic["observations"])
        self.assertEqual(diagnostic["acceptance_effect"], "NONE")
        self.assertFalse(MODULE.search_query_gates(negative, empty, current, 2000)["quality_passed"])
        for secret in (seed["query"], seed["expected_path_sha256"], "fixture.txt"):
            self.assertNotIn(secret, json.dumps(diagnostic))

    def test_repeat_classifies_only_complete_equal_score_hit_permutations(self):
        _, current = self.search_fixture()
        first = current["results"][0]
        second = {**first, "chunk_id": 2, "content": "private-body"}
        baseline = {**current, "total": 2, "results": [first, second]}
        reordered = {**baseline, "results": [second, first], "took_ms": 2001}
        snapshot = copy.deepcopy((baseline, reordered))
        result = MODULE.repeated_result_diagnostics(baseline, reordered)
        self.assertEqual(result["classification"], "IDENTICAL_HITS_EQUAL_SCORE_TIE_PERMUTATION")
        self.assertEqual(result["acceptance_effect"], "NONE")
        self.assertEqual((baseline, reordered), snapshot)
        self.assertNotIn("private-body", json.dumps(result))
        identical = MODULE.repeated_result_diagnostics(baseline, {**baseline, "took_ms": 5})
        self.assertEqual(identical["classification"], "ORDERED_RESULTS_IDENTICAL")

    def test_repeat_rejects_changed_hit_fields_totals_and_non_tie_order(self):
        _, current = self.search_fixture()
        first = current["results"][0]
        second = {**first, "chunk_id": 2, "score": 2}
        baseline = {**current, "total": 2, "results": [second, first]}
        alternatives = [
            {**baseline, "total": 3},
            {**current, "results": baseline["results"]},
            {**baseline, "results": [first, second]},
            {**baseline, "results": [second, {**first, "content": "changed"}]},
            {**baseline, "results": [second, {**first, "chunk_id": 3}]},
            {**baseline, "results": [second]},
        ]
        for changed in alternatives:
            with self.subTest(changed=changed):
                self.assertEqual(MODULE.repeated_result_diagnostics(baseline, changed)["classification"],
                                 "UNCLASSIFIED_VARIATION")
        with_boolean = {**current, "results": [{**first, "metadata": True}]}
        with_number = {**current, "results": [{**first, "metadata": 1}]}
        self.assertEqual(MODULE.repeated_result_diagnostics(with_boolean, with_number)["classification"],
                         "UNCLASSIFIED_VARIATION")

    def test_repeat_fails_closed_on_invalid_identities(self):
        _, current = self.search_fixture()
        first = current["results"][0]
        malformed = [{}, {"results": None}, {**current, "total": True},
                     {**current, "total": -1}, {**current, "results": [first, first]}]
        malformed += [{**current, "results": [{**first, field: value}]}
                      for field, value in (("chunk_id", True), ("chunk_id", 0), ("chunk_id", "1"),
                                           ("score", True), ("score", float("nan")),
                                           ("score", float("inf")), ("score", "1"))]
        for invalid in malformed:
            with self.subTest(invalid=invalid):
                self.assertEqual(MODULE.repeated_result_diagnostics(invalid, invalid)["classification"],
                                 "INVALID_RESULT_IDENTITY")

    def test_repeat_empty_results_are_identity_not_positive_quality(self):
        seed, _ = self.search_fixture()
        empty = {"results": [], "total": 0, "took_ms": 1}
        self.assertEqual(MODULE.repeated_result_diagnostics(empty, empty)["classification"],
                         "ORDERED_RESULTS_IDENTICAL")
        self.assertFalse(MODULE.search_query_gates(seed, empty, empty, 2000)["quality_passed"])

    def test_search_gates_preserve_exact_quality_and_latency_thresholds(self) -> None:
        seed, current = self.search_fixture()
        for millis, passed in ((0, True), (2000, True), (2001, False), (-1, False),
                               (True, False), ("1", False), (None, False)):
            result = MODULE.search_query_gates(seed, current, {**current, "took_ms": millis}, 2000)
            self.assertEqual(result["performance_passed"], passed)
            self.assertTrue(result["quality_passed"])
        extra = {**current["results"][0], "file_path": "extra.txt"}
        result = MODULE.search_query_gates(seed, current,
                                          {**current, "results": [*current["results"], extra]}, 2000)
        self.assertFalse(result["quality_passed"])
        self.assertEqual(result["missing_current_paths"], 1)
        self.assertEqual(result["missing_storage_v2_paths"], 0)
        self.assertNotIn("fixture.txt", json.dumps(result))
        self.assertNotIn("private-query", json.dumps(result))

    def test_search_gates_reject_missing_expected_reordered_and_degraded_hits(self) -> None:
        seed, current = self.search_fixture()
        other = {**current["results"][0], "file_path": "second.txt"}
        two = {**current, "results": [*current["results"], other]}
        reordered = {**two, "results": list(reversed(two["results"]))}
        self.assertFalse(MODULE.search_query_gates(seed, two, reordered, 2000)["quality_passed"])
        missing = MODULE.search_query_gates(seed, current, {"results": [], "took_ms": 1}, 2000)
        self.assertFalse(missing["quality_passed"])
        self.assertEqual(missing["missing_storage_v2_paths"], 1)
        degraded = {**current, "results": [{**current["results"][0], "degradation": {}}]}
        self.assertFalse(MODULE.search_query_gates(seed, current, degraded, 2000)["degradation_passed"])
        empty = {"results": [], "took_ms": 1}
        self.assertTrue(MODULE.search_query_gates({**seed, "expects_match": False}, empty, empty, 2000)
                        ["quality_passed"])
        self.assertFalse(MODULE.search_query_gates(seed, empty, empty, 2000)["quality_passed"])

    def test_failed_queries_persist_private_evidence_and_never_qualify(self) -> None:
        seed, current = self.search_fixture()
        for seeds in ([seed], []):
            with self.subTest(query_count=len(seeds)), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                arguments = Namespace(checkpoint=directory / "checkpoint.json",
                                      output=directory / "attempt.json", source_id=1,
                                      commit_sha="a" * 40, api_url="http://fixture.invalid", max_query_ms=2000)
                checkpoint = {"source_id": 1, "source_ref": "b" * 64, "commit_sha": "a" * 40,
                              "generation_id": 2, "generation_seq": 1, "item_count": 1,
                              "source_watermark_sha256": "c" * 64, "active_generation_id": None,
                              "server_instance_id": "before"}
                MODULE.atomic_private_json(arguments.checkpoint, checkpoint)
                repeated = {**checkpoint, "reused_generation": True,
                            "active_generation_before": None, "active_generation_after": None,
                            "telemetry": {}}
                verified = {**checkpoint, "status": "verified", "intelligence_export": {},
                            "query_seeds": seeds}
                with patch.object(MODULE, "source_state", return_value={"server_instance_id": "after"}), \
                     patch.object(MODULE, "validate_telemetry"), \
                     patch.object(MODULE, "verify_intelligence", return_value={}), \
                     patch.object(MODULE, "request", side_effect=[repeated, verified, current,
                                                                 {**current, "took_ms": 2001}, {}]) as request:
                    with self.assertRaisesRegex(RuntimeError, "candidate search"):
                        MODULE.verify(arguments, "private-token")
                    self.assertEqual(request.call_count, 5 if seeds else 2)
                    self.assertFalse(any("qualify" in call.args[3] or "dual-read" in call.args[3]
                                         for call in request.call_args_list))
                artifact = json.loads(arguments.output.read_text())
                self.assertEqual(artifact["status"], "FAIL")
                self.assertEqual(len(artifact["query_results"]), len(seeds))
                self.assertEqual(artifact["query_seed_summary"], MODULE.query_seed_summary(seeds))
                self.assertFalse(artifact["checks"]["performance"])
                self.assertFalse(artifact["qualification_submitted"])
                self.assertEqual(stat.S_IMODE(arguments.output.stat().st_mode), 0o600)
                self.assertNotIn("private-token", arguments.output.read_text())
                with self.assertRaisesRegex(RuntimeError, "output already exists"):
                    MODULE.verify(arguments, "private-token")

    def coverage_fixture(self):
        seed, current = self.search_fixture()
        seed["query"] = "private_query"
        candidate = {**current["results"][0], "chunk_id": 11, "external_hit_id": "storage-v2:one"}
        added = {**candidate, "chunk_id": 12, "file_path": "added.txt", "external_hit_id": "storage-v2:two"}
        storage = {"results": [candidate, added], "took_ms": 100}
        checkpoint = {"source_id": 1, "generation_id": 2, "generation_seq": 1, "commit_sha": "a" * 40}
        evidence = {**checkpoint, "schema_version": "mainrag.storage-v2.query-coverage.v1",
                    "query_sha256": MODULE.sha256_text(seed["query"]),
                    "candidate": [{"occurrence_id": row["chunk_id"], "external_hit_id": row["external_hit_id"],
                                   "path_sha256": MODULE.sha256_text(row["file_path"]),
                                   "body_sha256": "b" * 64, "body_text_matches": True,
                                   "reference_frequency": 2, "posting_frequency": 2}
                                  for row in storage["results"]],
                    "current": [{"chunk_id": 1, "path_sha256": MODULE.sha256_text("fixture.txt"),
                                 "indexed_match": True}],
                    "legacy_paths": [{"path_sha256": MODULE.sha256_text("fixture.txt"),
                                      "chunk_count": 1, "indexed_matches": 1, "literal_matches": 1},
                                     {"path_sha256": MODULE.sha256_text("added.txt"),
                                      "chunk_count": 0, "indexed_matches": 0, "literal_matches": 0}]}
        return seed, current, storage, evidence, checkpoint

    def test_transport_failures_keep_completed_and_pending_proof_without_qualifying(self) -> None:
        seed, current, storage, proof, identity = self.coverage_fixture()
        # Include bodies to prove pending results do not copy full response content.
        current["results"][0]["content"] = "private-pending-body"
        storage["results"][0]["content"] = "private-pending-body"
        for phase, extra, ordinal in (("search_current", [], 2),
                                      ("search_storage_v2", [current], 2),
                                      ("query_coverage", [current, storage], 2),
                                      ("dual_read", None, None),
                                      ("qualification", None, None)):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                arguments = Namespace(checkpoint=directory / "checkpoint.json", output=directory / "result.json",
                                      source_id=1, commit_sha="a" * 40, api_url="http://fixture.invalid",
                                      max_query_ms=2000, pack_root=directory, minimum_free_bytes=0)
                checkpoint = {**identity, "source_ref": "b" * 64, "item_count": 2,
                              "source_watermark_sha256": "c" * 64, "active_generation_id": None,
                              "server_instance_id": "before", "build": {"fixture_sha256": "d" * 64}}
                MODULE.atomic_private_json(arguments.checkpoint, checkpoint)
                repeated = {**checkpoint, "reused_generation": True, "telemetry": {},
                            "active_generation_before": None, "active_generation_after": None}
                verified = {**checkpoint, "status": "verified", "intelligence_export": {},
                            "query_seeds": [seed, seed] if ordinal else [seed],
                            "checks": {key: "PASS" for key in MODULE.CHECKS},
                            "adapter_profile_id": "fixture-adapter", "analysis_profile_id": "fixture-analysis",
                            "search_profile_id": "fixture-search"}
                replies = [repeated, verified, current, storage, proof, *(extra or [])]
                if phase == "qualification":
                    replies.append({"status": "PASS", "artifact": {"unexplained_count": 0},
                                    "evidence_id": "fixture-evidence", "artifact_sha256": "e" * 64})
                error = RuntimeError("private-token private-response")
                error.__cause__ = urllib.error.HTTPError("http://fixture.invalid/private", 408,
                                                        "private-token", {}, None)
                replies.append(error)
                intelligence = {"commands": ["card"], "result_sha256": {"card": "f" * 64}}
                with patch.object(MODULE, "source_state", return_value={"server_instance_id": "after"}), \
                     patch.object(MODULE, "validate_telemetry"), \
                     patch.object(MODULE, "verify_intelligence", return_value=intelligence), \
                     patch.object(MODULE, "request", side_effect=replies) as request:
                    with self.assertRaises(RuntimeError) as caught:
                        MODULE.verify(arguments, "private-token")
                    self.assertIs(caught.exception, error)
                artifact = json.loads(arguments.output.read_text())
                self.assertEqual(artifact["status"], "FAIL")
                self.assertEqual(artifact["failed_gate"], phase)
                self.assertEqual(artifact["checkpoint"], checkpoint)
                self.assertEqual(artifact["verification"], verified)
                self.assertEqual(artifact["query_seed_summary"],
                                 MODULE.query_seed_summary(verified["query_seeds"]))
                self.assertEqual(artifact["intelligence"], intelligence)
                self.assertEqual(artifact["query_coverage"], [proof])
                self.assertEqual(len(artifact["query_results"]), 1)
                self.assertEqual(len(artifact["comparisons"]), 1)
                self.assertEqual(artifact["error"], {"type": "RuntimeError", "http_status": 408})
                self.assertEqual(artifact["qualification_attempted"], phase == "qualification")
                self.assertEqual(artifact["qualification_outcome"],
                                 "UNKNOWN" if phase == "qualification" else "NOT_ATTEMPTED")
                if ordinal:
                    pending = artifact["pending_query"]
                    self.assertEqual(pending["ordinal"], ordinal)
                    self.assertEqual(pending["query_sha256"], MODULE.sha256_text(seed["query"]))
                    self.assertEqual("current" in pending, len(extra) >= 1)
                    self.assertEqual("storage_v2" in pending, len(extra) >= 2)
                    self.assertNotIn("query", pending)
                else:
                    self.assertNotIn("pending_query", artifact)
                if phase != "qualification":
                    self.assertFalse(any("qualify" in call.args[3] for call in request.call_args_list))
                if ordinal:
                    self.assertFalse(any("dual-read" in call.args[3] for call in request.call_args_list))
                self.assertEqual(stat.S_IMODE(arguments.output.stat().st_mode), 0o600)
                for private in ("private-token", "private-response", "private-pending-body"):
                    self.assertNotIn(private, arguments.output.read_text())
                original = arguments.output.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "output already exists"):
                    MODULE.verify(arguments, "private-token")
                self.assertEqual(arguments.output.read_bytes(), original)

    def test_initial_runtime_failure_retains_checkpoint_and_no_exception_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            arguments = Namespace(checkpoint=directory / "checkpoint.json", output=directory / "result.json",
                                  source_id=1, commit_sha="a" * 40, api_url="http://fixture.invalid")
            checkpoint = {"source_id": 1, "commit_sha": "a" * 40, "generation_seq": 1}
            MODULE.atomic_private_json(arguments.checkpoint, checkpoint)
            with patch.object(MODULE, "source_state", side_effect=RuntimeError("private-token")), \
                 patch.object(MODULE, "request") as request:
                with self.assertRaises(RuntimeError):
                    MODULE.verify(arguments, "private-token")
                request.assert_not_called()
            artifact = json.loads(arguments.output.read_text())
            self.assertEqual(artifact["failed_gate"], "restart_state")
            self.assertEqual(artifact["checkpoint"], checkpoint)
            self.assertEqual(artifact["error"], {"type": "RuntimeError"})
            self.assertEqual(artifact["query_results"], [])
            self.assertFalse(artifact["qualification_attempted"])
            self.assertNotIn("private-token", arguments.output.read_text())

    def test_intelligence_failure_retains_completed_command_hashes(self) -> None:
        layers = [{"generic_card": {"name": "private-symbol"}}]
        card = {"private": "card-content"}
        progress = {}
        with patch.object(MODULE, "request", side_effect=[layers, card, TimeoutError("private-token")]):
            with self.assertRaises(TimeoutError):
                MODULE.verify_intelligence("http://fixture.invalid", "private-token", 1, 1,
                                           {"payload": {"record_counts": {"cards": 1}}}, progress)
        self.assertEqual(progress["phase"], "intelligence_explain")
        self.assertEqual(progress["intelligence_result_sha256"], {
            "layers": MODULE.sha256_text(json.dumps(layers, sort_keys=True)),
            "card": MODULE.sha256_text(json.dumps(card, sort_keys=True)),
        })
        for private in ("private-token", "private-symbol", "card-content"):
            self.assertNotIn(private, json.dumps(progress))

    def test_additional_coverage_requires_identity_bound_body_and_term_proof(self) -> None:
        seed, current, storage, evidence, checkpoint = self.coverage_fixture()
        self.assertFalse(MODULE.search_query_gates(seed, current, storage, 2000)["quality_passed"])
        result = MODULE.search_query_gates(seed, current, storage, 2000, evidence, checkpoint)
        self.assertTrue(result["quality_passed"])
        self.assertEqual(result["coverage"]["additional_path_classes"], {"legacy_not_indexed": 1})
        for counts, classification in [((2, 0, 1), "legacy_lexical_projection_gap"),
                                        ((2, 0, 0), "legacy_content_gap"),
                                        ((2, 1, 1), "ranking_expansion")]:
            proof = copy.deepcopy(evidence)
            proof["legacy_paths"][1].update(zip(("chunk_count", "indexed_matches", "literal_matches"), counts))
            result = MODULE.query_coverage_gates(seed, current, storage, proof, checkpoint)
            self.assertTrue(result["passed"])
            self.assertEqual(result["additional_path_classes"], {classification: 1})

    def test_query_coverage_rejects_tampering_and_incomplete_support(self) -> None:
        seed, current, storage, evidence, checkpoint = self.coverage_fixture()
        mutations = [
            lambda p: p.update(source_id=2), lambda p: p.update(source_id=True),
            lambda p: p.update(generation_id=3), lambda p: p.update(generation_seq=2),
            lambda p: p.update(commit_sha="c" * 40), lambda p: p.update(query_sha256="d" * 64),
            lambda p: p.update(schema_version="unsupported"),
            lambda p: p["candidate"].pop(),
            lambda p: p["candidate"][0].update(occurrence_id=True),
            lambda p: p["candidate"][0].update(occurrence_id=0),
            lambda p: p["candidate"][0].update(external_hit_id="wrong"),
            lambda p: p["candidate"][0].update(path_sha256="d" * 64),
            lambda p: p["candidate"][0].update(body_text_matches=False),
            lambda p: p["candidate"][0].update(body_sha256="invalid"),
            lambda p: p["candidate"][0].update(reference_frequency=0),
            lambda p: p["candidate"][0].update(posting_frequency=1),
            lambda p: p["candidate"].append(p["candidate"][0]),
            lambda p: p["current"][0].update(indexed_match=False),
            lambda p: p["current"][0].update(chunk_id=2),
            lambda p: p["legacy_paths"].pop(),
            lambda p: p["legacy_paths"][0].update(indexed_matches=2),
            lambda p: p["legacy_paths"][0].update(literal_matches=-1),
        ]
        for ordinal, mutate in enumerate(mutations):
            with self.subTest(mutation=ordinal):
                proof = copy.deepcopy(evidence)
                mutate(proof)
                self.assertFalse(MODULE.query_coverage_gates(seed, current, storage, proof, checkpoint)["passed"])

    def test_supported_coverage_is_bound_into_dual_read_and_qualification(self) -> None:
        seed, current, storage, proof, identity = self.coverage_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            arguments = Namespace(checkpoint=directory / "checkpoint.json",
                                  output=directory / "result.json", source_id=1,
                                  commit_sha="a" * 40, api_url="http://fixture.invalid",
                                  max_query_ms=2000, pack_root=directory, minimum_free_bytes=0)
            checkpoint = {**identity, "source_ref": "b" * 64, "item_count": 2,
                          "source_watermark_sha256": "c" * 64, "active_generation_id": None,
                          "server_instance_id": "before", "build": {"fixture_sha256": "d" * 64}}
            MODULE.atomic_private_json(arguments.checkpoint, checkpoint)
            repeated = {**checkpoint, "reused_generation": True, "telemetry": {},
                        "active_generation_before": None, "active_generation_after": None}
            verified = {**checkpoint, "status": "verified", "intelligence_export": {},
                        "query_seeds": [seed], "checks": {key: "PASS" for key in MODULE.CHECKS},
                        "adapter_profile_id": "fixture-adapter", "analysis_profile_id": "fixture-analysis",
                        "search_profile_id": "fixture-search"}
            dual = {"status": "PASS", "artifact": {"unexplained_count": 0},
                    "evidence_id": "fixture-evidence", "artifact_sha256": "e" * 64}
            qualified = {**identity, "status": "release_candidate", "evidence_id": "fixture-qualified",
                         "active_generation_id": None}
            with patch.object(MODULE, "source_state", return_value={"server_instance_id": "after"}), \
                 patch.object(MODULE, "validate_telemetry"), \
                 patch.object(MODULE, "verify_intelligence", return_value={}), \
                 patch.object(MODULE, "publish_telemetry"), patch("builtins.print"), \
                 patch.object(MODULE, "request", side_effect=[repeated, verified, current, storage,
                                                             proof, dual, qualified]) as request:
                MODULE.verify(arguments, "private-token")
            calls = request.call_args_list
            self.assertEqual(len(calls), 7)
            self.assertEqual(calls[4].args[4]["candidate_occurrence_ids"], [11, 12])
            self.assertEqual(calls[4].args[4]["current_chunk_ids"], [1])
            self.assertEqual(calls[4].args[4]["query"], seed["query"])
            dual_request = calls[5].args[4]
            comparisons = dual_request["queries"]
            self.assertEqual(comparisons[0]["fixture"]["coverage_evidence_sha256"],
                             MODULE.sha256_text(json.dumps(proof, sort_keys=True)))
            self.assertEqual(dual_request["query_set_sha256"], MODULE.query_set_sha256(comparisons))
            manifest = calls[6].args[4]["manifest"]
            self.assertEqual(manifest["query_coverage_sha256"],
                             MODULE.sha256_text(json.dumps([proof], sort_keys=True)))
            self.assertTrue(manifest["query_results"][0]["coverage"]["all_candidate_hits_supported"])
            artifact = json.loads(arguments.output.read_text())
            self.assertEqual(artifact["query_coverage"], [proof])
            self.assertEqual(artifact["result"], qualified)
            self.assertEqual(stat.S_IMODE(arguments.output.stat().st_mode), 0o600)
            self.assertNotIn("private-token", arguments.output.read_text())

    def test_proven_new_hits_may_not_displace_or_reorder_baseline_paths(self) -> None:
        seed, current, storage, evidence, checkpoint = self.coverage_fixture()
        current["results"].append({**current["results"][0], "chunk_id": 2, "file_path": "added.txt"})
        evidence["current"].append({"chunk_id": 2, "path_sha256": MODULE.sha256_text("added.txt"),
                                    "indexed_match": True})
        evidence["legacy_paths"][1].update(chunk_count=1, indexed_matches=1, literal_matches=1)
        self.assertTrue(MODULE.query_coverage_gates(seed, current, storage, evidence, checkpoint)["passed"])
        storage["results"].reverse()
        self.assertFalse(MODULE.query_coverage_gates(seed, current, storage, evidence, checkpoint)["passed"])
        storage["results"].pop(0)
        evidence["candidate"].pop()
        self.assertFalse(MODULE.query_coverage_gates(seed, current, storage, evidence, checkpoint)["passed"])

    def test_release_candidate_telemetry_requires_fragment_bounds(self) -> None:
        telemetry = {
            "phase": {name: 1.0 for name in MODULE.TELEMETRY_PHASES},
            "ablauf": {name: 0 for name in MODULE.TELEMETRY_COUNTERS},
        }
        telemetry["ablauf"].update(
            eingang_bytes=1024,
            io_buffer_bytes=65536,
            largest_item_bytes=512,
            fragments_created=2,
        )
        MODULE.validate_telemetry(telemetry, 3)
        telemetry["ablauf"]["fragments_created"] = 4
        with self.assertRaisesRegex(RuntimeError, "fragment count"):
            MODULE.validate_telemetry(telemetry, 3)

    def test_query_set_identity_is_order_independent(self) -> None:
        first = {"fixture": {"id": "a", "query": "alpha", "phrase": False, "k": 10}}
        second = {"fixture": {"id": "b", "query": "beta", "phrase": False, "k": 10}}
        self.assertEqual(
            MODULE.query_set_sha256([first, second]),
            MODULE.query_set_sha256([second, first]),
        )

    def test_ranked_maps_storage_hits_to_legacy_ids_by_path_hash(self) -> None:
        result = {"external_hit_id": "storage:1", "chunk_id": 7, "file_path": "same.rs", "score": 1.0}
        path_hash = MODULE.sha256_text("same.rs")
        ranked = MODULE.ranked([result], {path_hash: ["legacy:3", "legacy:4"]})
        self.assertEqual(ranked[0]["mapped_hit_ids"], ["legacy:3", "legacy:4"])
        self.assertEqual(ranked[0]["rank"], 1)

    def test_path_identity_preserves_rank_and_deduplicates_segments(self) -> None:
        results = [
            {"file_path": "first.rs"},
            {"file_path": "first.rs"},
            {"file_path": "second.rs"},
        ]
        self.assertEqual(
            MODULE.path_identity(results),
            [MODULE.sha256_text("first.rs"), MODULE.sha256_text("second.rs")],
        )

    def test_atomic_evidence_is_private_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            MODULE.atomic_private_json(path, {"status": "PASS"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), {"status": "PASS"})


if __name__ == "__main__":
    unittest.main()
