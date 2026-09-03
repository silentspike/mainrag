"""Focused unit checks for the protected release-candidate operator."""

from __future__ import annotations

import importlib.util
import json
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "ops" / "storage-v2" / "release-candidate.py"
SPEC = importlib.util.spec_from_file_location("release_candidate_operator", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseCandidateOperatorTests(unittest.TestCase):
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
