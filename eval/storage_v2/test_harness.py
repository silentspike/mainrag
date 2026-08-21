"""Focused tests for the public storage-v2 baseline harness."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

EVAL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVAL_ROOT))

from eval_common import percentile, recall_at_k, reciprocal_rank
from storage_v2 import harness
from storage_v2.check_writers import discover_candidates
from storage_v2.compare_manifests import compare, relative_delta


class CommonMetricTests(unittest.TestCase):
    def test_deterministic_recall_and_reciprocal_rank(self) -> None:
        results = ["z.md", "nested/alpha.rs", "beta.md"]
        self.assertEqual(recall_at_k(results, ["alpha.rs", "beta.md"], 10), 1.0)
        self.assertEqual(reciprocal_rank(results, ["alpha.rs"], 10), 0.5)

    def test_negative_case_requires_empty_results(self) -> None:
        self.assertEqual(recall_at_k([], [], 10), 1.0)
        self.assertEqual(recall_at_k(["unexpected.md"], [], 10), 0.0)

    def test_percentile_rejects_zero_samples(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty sample"):
            percentile([], 95)


class FixtureContractTests(unittest.TestCase):
    def test_frozen_suite_covers_every_required_construct(self) -> None:
        queries = harness.load_queries()
        constructs = {query["construct"] for query in queries}
        self.assertTrue(
            {"and", "or", "not", "phrase", "group", "exact_identifier", "adverse"}
            <= constructs
        )
        self.assertGreater(len(queries), 0)

    def test_zero_query_suite_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "empty.jsonl"
            path.write_text("\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "zero queries"):
                harness.load_queries(path)

    def test_corpus_hash_is_stable_and_order_sensitive(self) -> None:
        first = [("a", b"one"), ("b", b"two")]
        self.assertEqual(harness.canonical_corpus_hash(first), harness.canonical_corpus_hash(first))
        self.assertNotEqual(
            harness.canonical_corpus_hash(first),
            harness.canonical_corpus_hash(list(reversed(first))),
        )

    def test_query_sql_has_deterministic_tie_break_and_no_candidate_cap(self) -> None:
        sql = harness.query_sql("safe query", phrase=False)
        self.assertIn("ORDER BY score DESC, path ASC, id ASC", sql)
        self.assertIn("LIMIT 10", sql)
        self.assertNotIn("LIMIT 500", sql)


class ManifestContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(harness.SCHEMA.read_text(encoding="utf-8"))
        cls.validator = jsonschema.Draft202012Validator(cls.schema)

    def test_malformed_manifest_is_rejected(self) -> None:
        errors = list(self.validator.iter_errors({"status": "PASS"}))
        self.assertGreater(len(errors), 0)

    def test_status_vocabulary_preserves_non_pass_states(self) -> None:
        values = set(self.schema["$defs"]["status"]["enum"])
        self.assertEqual(values, {"PASS", "FAIL", "BLOCKED", "SKIP", "NOT_RUN"})

    def test_redaction_rejects_local_paths_and_secret_shaped_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "local operational value"):
            harness.ensure_public_manifest({"evidence": "/private/result.json"})
        with self.assertRaisesRegex(ValueError, "private field name"):
            harness.ensure_public_manifest({"api_token": "redacted"})


class WriterInventoryTests(unittest.TestCase):
    def test_discovers_unlisted_write_signal(self) -> None:
        candidates = discover_candidates(
            {"api/src/services/new_writer.rs": 'sql("INSERT INTO chunks VALUES (1)")'}
        )
        self.assertEqual(candidates, {"api/src/services/new_writer.rs"})


class RepeatComparisonTests(unittest.TestCase):
    @staticmethod
    def manifest() -> dict:
        query = {
            "id": "q",
            "construct": "and",
            "query_sha256": "a" * 64,
            "status": "PASS",
            "expected": ["a.md"],
            "exact_top_10": ["a.md"],
            "recall_at_10": 1.0,
            "reciprocal_rank": 1.0,
            "matched_documents": 1,
            "scored_channel_rows": 2,
            "returned_shortlist": 1,
            "cold_first_ms": 10.0,
            "warm_latency": {},
        }
        return {
            "status": "PASS",
            "subject": {"code_sha": "a" * 40},
            "inputs": {"corpus_sha256": "b" * 64},
            "configuration": {"concurrency": 1},
            "maintenance_gate": {"checked": [{"path": "writer.rs"}]},
            "ingest": {
                "source_bytes_read": 1,
                "content_bytes_stored": 1,
                "parsed_items": 1,
                "unchanged_items_reused": 1,
                "errors": 0,
                "database_bytes_after_ingest": 1,
            },
            "search": {
                "query_count": 1,
                "recall_at_10": 1.0,
                "mrr_at_10": 1.0,
                "result_identity_sha256": "c" * 64,
                "matched_documents_total": 1,
                "scored_channel_rows_total": 2,
                "returned_shortlist_total": 1,
                "queries": [query],
                "warm_latency": {"p50_ms": 10.0, "p95_ms": 12.0, "p99_ms": 14.0},
            },
        }

    def test_comparison_accepts_equal_identity_with_timing_noise(self) -> None:
        left = self.manifest()
        right = self.manifest()
        right["search"]["warm_latency"] = {"p50_ms": 11.0, "p95_ms": 13.0, "p99_ms": 15.0}
        self.assertEqual(compare(left, right, 0.5), [])

    def test_comparison_rejects_result_identity_drift(self) -> None:
        left = self.manifest()
        right = self.manifest()
        right["search"]["result_identity_sha256"] = "d" * 64
        self.assertIn("exact field differs: search.result_identity_sha256", compare(left, right, 0.5))

    def test_relative_timing_delta_is_symmetric(self) -> None:
        self.assertEqual(relative_delta(10, 12), relative_delta(12, 10))


if __name__ == "__main__":
    unittest.main()
