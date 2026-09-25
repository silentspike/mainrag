"""Safety and privacy checks for the protected issue-66 source inventory."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/candidate-inventory.py"
SPEC = importlib.util.spec_from_file_location("candidate_inventory", PATH)
assert SPEC and SPEC.loader
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)


def source(source_id: int = 1) -> dict:
    return {
        "source_id": source_id,
        "name": "private-source-name",
        "source_type": "fs",
        "path": "/private/source-path",
        "config": {"private": "credential-like-config"},
        "is_test": False,
        "last_synced": None,
        "updated_at": "2026-09-24T00:00:00Z",
        "file_count": 2,
        "total_size": 100,
        "active_generation_id": None,
        "next_generation_seq": 2,
        "generations": [{"generation_id": 5, "generation_seq": 1,
                         "status": "release_candidate", "item_count": 2,
                         "verification_manifest_sha256": "a" * 64,
                         "evidence_id": "fixture-evidence", "commit_sha": "b" * 40,
                         "source_watermark_sha256": "c" * 64,
                         "adapter_profile_id": "fixture-adapter",
                         "analysis_profile_id": "fixture-analysis",
                         "search_profile_id": "fixture-search",
                         "qualification_manifest": {"status": "PASS", "checks": {}},
                         "qualification_manifest_sha256": "d" * 64,
                         "qualification_manifest_digest_matches": True}],
    }


class CandidateInventoryTests(unittest.TestCase):
    def test_mixed_commit_map_binds_each_live_generation(self) -> None:
        second = source(2)
        second["generations"][0].update(generation_id=6, commit_sha="c" * 40)
        commit_map = {
            "schema_version": "mainrag.storage-v2.final-candidate-commit-map.v1",
            "sources": [
                {"source_id": 1, "candidate_generation_id": 5,
                 "candidate_commit_sha": "b" * 40},
                {"source_id": 2, "candidate_generation_id": 6,
                 "candidate_commit_sha": "c" * 40},
            ],
        }
        protected, public = INVENTORY.capture(
            [source(), second], "d" * 40, None, commit_map, "e" * 64)
        self.assertEqual(protected["schema_version"],
                         "mainrag.storage-v2.candidate-inventory.v2")
        self.assertEqual(public["release_candidate_source_count"], 2)
        with self.assertRaisesRegex(RuntimeError, "differs from live inventory"):
            INVENTORY.capture([source(), second], "d" * 40, None,
                              {**commit_map, "sources": [
                                  commit_map["sources"][0],
                                  {**commit_map["sources"][1],
                                   "candidate_commit_sha": "f" * 40}]}, "e" * 64)

    def test_public_summary_contains_only_opaque_references_and_counts(self) -> None:
        second = source(2)
        second.update(name="another-private-name", path="/another/private-path",
                      source_type="private-custom-type", is_test=True, generations=[])
        protected, public = INVENTORY.capture([source(), second], "d" * 40, "b" * 40)
        self.assertEqual(protected["operator_commit_sha"], "d" * 40)
        self.assertEqual(protected["candidate_commit_sha"], "b" * 40)
        self.assertEqual(public["source_count"], 2)
        self.assertEqual(public["test_source_count"], 1)
        self.assertEqual(public["release_candidate_source_count"], 1)
        self.assertEqual(public["sources_without_candidate_count"], 1)
        self.assertEqual(public["source_type_counts"], {"fs": 1, "other": 1})
        self.assertEqual(len(set(public["source_refs"])), 2)
        self.assertEqual(public["capture_status"], "OBSERVED_ONLY")
        serialized = json.dumps(public)
        for private in ("private-source-name", "private-path", "credential-like-config",
                        "private-custom-type", '"source_id"', '"source_watermark_sha256"'):
            self.assertNotIn(private, serialized)
        self.assertEqual(protected["sources"][0]["name"], "private-source-name")
        self.assertEqual(public["protected_sha256"], INVENTORY.hashlib.sha256(
            INVENTORY.canonical(protected) + b"\n").hexdigest())

    def test_incomplete_or_inconsistent_snapshot_fails_closed(self) -> None:
        cases = [
            [source(), source()],
            [{**source(), "is_test": None}],
            [{**source(), "active_generation_id": 6}],
            [{**source(), "generations": [{**source()["generations"][0], "evidence_id": None}]}],
            [{**source(), "generations": [{**source()["generations"][0],
                                          "adapter_profile_id": None}]}],
            [{**source(), "generations": [*source()["generations"], *source()["generations"]]}],
            [{**source(), "generations": [
                source()["generations"][0],
                {**source()["generations"][0], "status": "verified"}]}],
        ]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                INVENTORY.capture(rows, "d" * 40, "b" * 40)
        with self.assertRaises(RuntimeError):
            INVENTORY.capture([], "d" * 40, "b" * 40)
        with self.assertRaisesRegex(RuntimeError, "candidate package commit"):
            INVENTORY.capture([source()], "d" * 40, "invalid")

    def test_private_snapshot_is_create_only_and_mode_600(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "evidence" / "inventory.json"
            INVENTORY.private_create(destination, {"private": "first"})
            original = destination.read_bytes()
            with self.assertRaises(FileExistsError):
                INVENTORY.private_create(destination, {"private": "second"})
            self.assertEqual(destination.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(sorted(path.name for path in destination.parent.iterdir()),
                             [destination.name])

    def test_database_query_is_read_only_and_does_not_surface_private_error(self) -> None:
        with patch.object(INVENTORY.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, "private-output", "private-database-error")) as run:
            with self.assertRaisesRegex(RuntimeError, "read-only source inventory query failed") as caught:
                INVENTORY.read_sources("fixture")
        self.assertNotIn("private-", str(caught.exception))
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertIn("-qAt", command)
        self.assertIn("default_transaction_read_only=on", environment["PGOPTIONS"])
        self.assertIn("row_security=off", environment["PGOPTIONS"])


if __name__ == "__main__":
    unittest.main()
