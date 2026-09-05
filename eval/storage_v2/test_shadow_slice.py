from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from storage_v2.shadow_slice import (
    comparable,
    exhaustive_fixture_scores,
    has_complete_search_bindings,
    matches_exhaustive_reference,
    publish_telemetry,
    query_set_sha256,
    ranked,
    storage_v2_query,
    validate_fixture_ingest_result,
)


class ShadowSliceHarnessTests(unittest.TestCase):
    def test_view_and_search_document_counts_need_not_match(self) -> None:
        for views, documents in ((2, 1), (1, 2), (0, 0)):
            self.assertTrue(has_complete_search_bindings({
                "view_count": views, "search_document_count": documents,
                "unbound_view_count": 0, "search_binding_error_count": 0,
            }))

    def test_missing_malformed_and_incomplete_bindings_fail_closed(self) -> None:
        complete = {"view_count": 2, "search_document_count": 2,
                    "unbound_view_count": 0, "search_binding_error_count": 0}
        for key in complete:
            state = dict(complete)
            del state[key]
            self.assertFalse(has_complete_search_bindings(state), key)
            for invalid in (None, True, False, "0", 0.0, -1):
                state[key] = invalid
                self.assertFalse(has_complete_search_bindings(state), key)
        for key in ("unbound_view_count", "search_binding_error_count"):
            self.assertFalse(has_complete_search_bindings({**complete, key: 1}), key)

    def test_query_set_digest_is_sorted_and_length_framed(self) -> None:
        queries = [
            {"id": "b", "query": "beta", "phrase": False},
            {"id": "a", "query": "alpha", "phrase": True},
        ]
        digest = hashlib.sha256()
        for value in sorted(
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
            for item in queries
        ):
            digest.update(struct.pack(">Q", len(value)))
            digest.update(value)
        self.assertEqual(query_set_sha256(queries), digest.hexdigest())
        self.assertEqual(query_set_sha256(list(reversed(queries))), digest.hexdigest())
        changed = [{**queries[0], "phrase": True}, queries[1]]
        self.assertNotEqual(query_set_sha256(queries), query_set_sha256(changed))

    def test_fixture_metadata_maps_to_explicit_storage_v2_syntax(self) -> None:
        self.assertEqual(
            storage_v2_query({"query": "atomic activation", "phrase": True}),
            '"atomic activation"',
        )
        self.assertEqual(
            storage_v2_query(
                {
                    "query": "active_generation_id",
                    "phrase": False,
                    "construct": "exact_identifier",
                }
            ),
            "id:active_generation_id",
        )

    def test_exhaustive_fixture_gate_checks_membership_score_and_identity_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.md").write_text("immutable generation", encoding="utf-8")
            (root / "b.md").write_text("immutable mutable generation", encoding="utf-8")
            (root / ".mainrag-shadow-fixture").write_text("ignored", encoding="utf-8")
            scores = exhaustive_fixture_scores(root, "immutable generation")
            results = [
                {
                    "file_path": path,
                    "score": score,
                    "external_hit_id": f"hit-{path}",
                    "chunk_id": index,
                }
                for index, (path, score) in enumerate(scores.items(), 1)
            ]
            results.sort(
                key=lambda result: (
                    -scores[result["file_path"]],
                    result["external_hit_id"],
                    result["chunk_id"],
                )
            )
            matched, expected = matches_exhaustive_reference(results, scores, 10)
            self.assertTrue(matched)
            self.assertEqual([result["file_path"] for result in results], expected)
            results[0]["score"] += 0.1
            self.assertFalse(matches_exhaustive_reference(results, scores, 10)[0])

    def test_ranked_results_emit_only_redacted_comparison_fields(self) -> None:
        result = {
            "chunk_id": 7,
            "score": 0.5,
            "file_path": "/not/serialized/private.txt",
            "content": "not serialized",
        }
        self.assertEqual(
            ranked([result], True),
            [
                {
                    "hit_id": "legacy:7",
                    "rank": 1,
                    "score": 0.5,
                    "mapped_hit_ids": [],
                    "authorized": True,
                }
            ],
        )
        self.assertNotIn("content", comparable(result, True))

    def test_api_telemetry_is_published_for_run_script_and_html(self) -> None:
        telemetry = {
            "phase": {
                "lesen_hashen_ms": 1.0,
                "content_store_ms": 2.0,
                "strukturprojektion_ms": 3.0,
                "analyse_ms": 4.0,
                "db_staging_ms": 5.0,
                "intervall_delta_ms": 6.0,
                "sealing_ms": 7.0,
            },
            "ablauf": {
                "latenz_ms": 28.0,
                "eingang_bytes": 100,
                "unique_bytes": 80,
                "stored_bytes": 40,
                "reuse_bodies": 1,
                "reuse_nodes": 2,
                "reuse_views": 3,
                "reuse_analysis": 4,
                "reuse_generation": 0,
                "parser_passes": 1,
                "analysis_retries": 1,
                "artifacts_created": 2,
                "occurrences_created": 2,
                "intervals_opened": 2,
                "intervals_closed": 0,
                "errors": 0,
                "io_buffer_bytes": 100,
                "peak_buffer_bytes": 100,
                "writer_concurrency": 1,
                "fragments_created": 0,
                "largest_item_bytes": 20,
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "kennzahlen.json"
            self.assertEqual(publish_telemetry(telemetry, str(output)), output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), telemetry)

    def test_incomplete_telemetry_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "phase keys"):
            publish_telemetry({"phase": {}, "ablauf": {}}, None)

    def test_fixture_result_reconciles_cold_return_and_noop(self) -> None:
        cold = {
            "item_count": 13,
            "generation_seq": 1,
            "controlled_retry_count": 1,
            "reused_generation": False,
            "telemetry": {
                "ablauf": {
                    "latenz_ms": 10,
                    "eingang_bytes": 100,
                    "unique_bytes": 100,
                    "stored_bytes": 80,
                    "reuse_bodies": 0,
                    "reuse_nodes": 0,
                    "reuse_views": 0,
                    "reuse_analysis": 0,
                    "reuse_generation": 0,
                    "parser_passes": 13,
                    "analysis_retries": 1,
                    "artifacts_created": 13,
                    "occurrences_created": 13,
                    "intervals_opened": 13,
                    "intervals_closed": 0,
                    "errors": 0,
                    "io_buffer_bytes": 65536,
                    "peak_buffer_bytes": 65536,
                    "writer_concurrency": 1,
                    "fragments_created": 0,
                    "largest_item_bytes": 10,
                }
            },
        }
        validate_fixture_ingest_result(cold)

        returned = json.loads(json.dumps(cold))
        returned.update(generation_seq=3, controlled_retry_count=0)
        returned["telemetry"]["ablauf"].update(
            unique_bytes=0,
            stored_bytes=0,
            reuse_bodies=13,
            reuse_nodes=13,
            reuse_views=13,
            reuse_analysis=13,
            parser_passes=0,
            analysis_retries=0,
            artifacts_created=0,
            occurrences_created=0,
            intervals_opened=1,
            intervals_closed=1,
        )
        validate_fixture_ingest_result(returned)

        noop = json.loads(json.dumps(returned))
        noop["reused_generation"] = True
        noop["telemetry"]["ablauf"].update(
            reuse_generation=1,
            intervals_opened=0,
            intervals_closed=0,
            peak_buffer_bytes=0,
            writer_concurrency=0,
        )
        validate_fixture_ingest_result(noop)

    def test_fixture_result_rejects_false_cold_interval_closures(self) -> None:
        invalid = {
            "item_count": 1,
            "generation_seq": 1,
            "controlled_retry_count": 1,
            "reused_generation": False,
            "telemetry": {
                "ablauf": {
                    "latenz_ms": 10,
                    "eingang_bytes": 10,
                    "unique_bytes": 10,
                    "stored_bytes": 8,
                    "reuse_bodies": 0,
                    "reuse_nodes": 0,
                    "reuse_views": 0,
                    "reuse_analysis": 0,
                    "reuse_generation": 0,
                    "parser_passes": 1,
                    "analysis_retries": 1,
                    "artifacts_created": 1,
                    "occurrences_created": 1,
                    "intervals_opened": 1,
                    "intervals_closed": 1,
                    "errors": 0,
                    "io_buffer_bytes": 65536,
                    "peak_buffer_bytes": 65536,
                    "writer_concurrency": 1,
                    "fragments_created": 0,
                    "largest_item_bytes": 10,
                }
            },
        }
        with self.assertRaisesRegex(RuntimeError, "initial fixture generation"):
            validate_fixture_ingest_result(invalid)


if __name__ == "__main__":
    unittest.main()
