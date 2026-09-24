"""Contract tests use synthetic observations, not performance evidence."""

import copy
import json
import unittest
import tempfile
from pathlib import Path

from eval.storage_v2 import harness
from eval.storage_v2 import supported_baseline as baseline


def observation():
    documents = harness.load_documents()
    size = sum(len(body) for _, body in documents)
    reads = sum(len(body) + min(len(body), 512) for _, body in documents)
    count = len(documents)
    return {
        "schema_version": "supported-ingest-observation/v1",
        "scope": "supported_eager_filesystem_cpu_ingest_and_chunk_fts",
        "corpus_sha256": harness.canonical_corpus_hash(documents), "corpus_items": count,
        "query_set_sha256": harness.sha256_file(harness.QUERIES),
        "query_sql_sha256": harness.sha256_file(harness.HERE / "current_path_query.sql"),
        "schema_columns_sha256": "a"*64, "backend_version": "18.3",
        "fixture_definition_sha256": baseline.fixture_definition_sha256(),
        "configuration": baseline.expected_configuration("hosted-ci-local-postgres"), "stable_chunk_count": count,
        "vector_connection_attempts": 0, "outbox_rows": 0, "ledger_rows": 2,
        "ingest": [{"phase": phase, "status": "PASS", "logical_input_bytes": size,
                    "files_processed": 0 if i else count, "files_skipped": count if i else 0,
                    "chunks_created": 0 if i else count, "chunk_compressed_bytes": 100,
                    "errors": 0, "elapsed_ms": 1.0,
                    "work": {"chunker_calls": 0 if i else count, "intelligence_parser_calls": 0 if i else count},
                    "source_io": {"content_read_coverage": "COMPLETE", "adapter_read_bytes": reads,
                                  "total_content_read_bytes": reads, "deferred_read_bytes": 0, "device_read_bytes": None,
                                  "excluded": ["filesystem_metadata", "walker_configuration", "pack_reads", "device_io"]}}
                   for i, phase in enumerate(("initial", "unchanged"))],
        "queries": [{"id": q["id"], "first_ms": 1.0, "warm_samples_ms": [1.0]*30,
                     "observation": {"results": q["expected"], "matched_documents": len(q["expected"]),
                                     "scored_channel_rows": 2*len(q["expected"])}}
                    for q in harness.load_queries()],
    }


class SupportedBaselineTests(unittest.TestCase):
    def test_execution_profile_preserves_observed_lexical_version(self):
        raw = observation()
        hosted = self.summarize(raw)
        self.assertEqual(hosted["configuration"]["indexed_lexical_version"], "hf_bge_wordpiece")
        with self.assertRaisesRegex(ValueError, "configuration drift"):
            baseline.summarize(raw, "b"*40, "remote-test-fixture-tunnel")
        raw["configuration"] = baseline.expected_configuration("remote-test-fixture-tunnel")
        remote = baseline.summarize(raw, "b"*40, "remote-test-fixture-tunnel")
        self.assertEqual(remote["configuration"]["indexed_lexical_version"], "tiktoken-cl100k")
        self.assertIn("identity differs: execution_profile", baseline.compare(hosted, remote, 0.5))

    def test_missing_and_failed_runs_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.log"
            missing = baseline.collect_report([path, path], "b"*40, "hosted-ci-local-postgres", 0.5)
            self.assertEqual(missing["status"], "NOT_RUN")
            baseline.validate_report(missing)
            path.write_text("test result: FAILED. 0 passed; 1 failed; 0 ignored;")
            failed = baseline.collect_report([path, path], "b"*40, "hosted-ci-local-postgres", 0.5)
            self.assertEqual(failed["status"], "FAIL")
            baseline.validate_report(failed)

    def test_schema_and_semantics_reject_forged_aggregate_pass(self):
        result = self.summarize()
        report = {"status": "PASS", "timing_tolerance": 0.5, "differences": [], "runs": [result, copy.deepcopy(result)]}
        report["runs"][1]["code_sha"] = "c"*40
        with self.assertRaisesRegex(ValueError, "differences were altered"):
            baseline.validate_report(report)

    def summarize(self, raw=None):
        return baseline.summarize(raw or observation(), "b"*40, "hosted-ci-local-postgres")

    def test_both_runs_and_exact_results_are_comparable(self):
        result = self.summarize()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["warm_latency"]["samples"], 330)
        self.assertEqual(baseline.compare(result, copy.deepcopy(result), 0.5), [])
        baseline.validate_report({"status": "PASS", "timing_tolerance": 0.5, "differences": [], "runs": [result, copy.deepcopy(result)]})

    def test_sql_row_aliases_and_logical_bytes_cannot_replace_observations(self):
        for field in ("work", "source_io"):
            raw = observation()
            del raw["ingest"][0][field]
            raw["ingest"][0]["parsed_items"] = raw["corpus_items"]
            with self.assertRaises(ValueError):
                self.summarize(raw)
        raw = observation()
        raw["ingest"][0]["source_io"]["total_content_read_bytes"] = raw["ingest"][0]["logical_input_bytes"]
        with self.assertRaises(ValueError):
            self.summarize(raw)

    def test_noop_still_requires_observed_reads_and_zero_parser_work(self):
        for field, value in (("adapter_read_bytes", 0), ("device_read_bytes", 0), ("content_read_coverage", "PARTIAL")):
            raw = observation()
            raw["ingest"][1]["source_io"][field] = value
            with self.assertRaises(ValueError):
                self.summarize(raw)
        raw = observation()
        raw["ingest"][1]["work"]["chunker_calls"] = False
        with self.assertRaises(ValueError):
            self.summarize(raw)

    def test_missing_queries_and_nonfinite_or_short_samples_fail(self):
        for samples in ([], [1.0]*29, [float("nan")]*30, [float("inf")]*30, [True]*30):
            raw = observation()
            raw["queries"][0]["warm_samples_ms"] = samples
            with self.assertRaises(ValueError):
                self.summarize(raw)
        raw = observation()
        raw["queries"] = []
        with self.assertRaises(ValueError):
            self.summarize(raw)

    def test_identity_config_and_private_result_drift_fail(self):
        for key in ("corpus_sha256", "query_set_sha256", "query_sql_sha256"):
            raw = observation()
            raw[key] = "0"*64
            with self.assertRaises(ValueError):
                self.summarize(raw)
        raw = observation()
        raw["configuration"]["cpu_mode"] = False
        with self.assertRaises(ValueError):
            self.summarize(raw)
        raw = observation()
        raw["queries"][0]["observation"]["results"] = ["private.md"]
        with self.assertRaises(ValueError):
            self.summarize(raw)

    def test_quality_failure_is_preserved_in_report(self):
        raw = observation()
        raw["queries"][0]["observation"]["results"] = []
        report = self.summarize(raw)
        self.assertEqual(report["status"], "FAIL")
        self.assertIn("both runs must pass", baseline.compare(report, report, 0.5))

    def test_work_and_latency_differences_are_not_hidden(self):
        first = self.summarize()
        second = copy.deepcopy(first)
        second["ingest"][1]["source_io"]["total_content_read_bytes"] += 1
        second["warm_latency"]["p95_ms"] *= 2
        errors = baseline.compare(first, second, 0.5)
        self.assertIn("measured ingestion work differs", errors)
        self.assertIn("warm latency tolerance exceeded: p95_ms", errors)

    def test_nonzero_test_completion_and_exactly_one_observation_required(self):
        row = "corpus baseline: " + json.dumps(observation())
        success = row + "\ntest result: ok. 1 passed; 0 failed; 0 ignored;"
        self.assertEqual(baseline.from_log(success), observation())
        for text in (row, success+"\n"+row, "0 passed; 0 failed; 0 ignored;"):
            with self.assertRaises(ValueError):
                baseline.from_log(text)
