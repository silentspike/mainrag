"""Final source-local delta planning and qualification readback contracts."""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/final-delta.py"
SPEC = importlib.util.spec_from_file_location("storage_v2_final_delta", PATH)
assert SPEC and SPEC.loader
DELTA = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DELTA)


def baseline() -> tuple[dict, list[dict], dict[int, dict]]:
    candidates = [
        {"source_id": source_id, "candidate_generation_id": generation_id,
         "candidate_generation_seq": 1,
         "expected_active_generation_id": None,
         "candidate_commit_sha": "b" * 40,
         "source_watermark_sha256": "a" * 64,
         "adapter_profile_id": "fixture-adapter",
         "evidence_id": f"fixture-{source_id}"}
        for source_id, generation_id in ((1, 11), (2, 22))
    ]
    audit = {
        "schema_version": "mainrag.storage-v2.candidate-aggregate-audit.v2",
        "persisted_candidate_set_complete": True,
        "candidate_set": candidates,
        "candidate_set_sha256": DELTA.ACTIVATION.sha256(DELTA.ACTIVATION.canonical(candidates)),
    }
    rows = [
        {"source_id": source_id, "active_generation_id": None,
         "generations": [{"generation_id": generation_id,
                          "generation_seq": 1, "status": "release_candidate",
                          "item_count": 1,
                          "commit_sha": "b" * 40,
                          "evidence_id": f"fixture-{source_id}",
                          "source_watermark_sha256": "a" * 64,
                          "adapter_profile_id": "fixture-adapter"}]}
        for source_id, generation_id in ((1, 11), (2, 22))
    ]
    observed = {
        source_id: {"source_id": source_id, "adapter_profile_id": "fixture-adapter",
                    "source_watermark_sha256": "a" * 64 if source_id == 1 else "c" * 64,
                    "item_count": 1, "input_bytes": 12, "application_read_bytes": 24}
        for source_id in (1, 2)
    }
    return audit, rows, observed


class FinalDeltaOperatorTests(unittest.TestCase):
    def test_observation_preserves_unmeasured_adapter_reads(self) -> None:
        value = baseline()[2][1]
        value["application_read_bytes"] = None

        class Opener:
            def open(self, request, timeout):
                return io.BytesIO(json.dumps(value).encode())

        with patch.object(DELTA.urllib.request, "build_opener", return_value=Opener()):
            self.assertIsNone(DELTA.observation("http://127.0.0.1:3001", "private", 1)[
                "application_read_bytes"])
            del value["application_read_bytes"]
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                DELTA.observation("http://127.0.0.1:3001", "private", 1)

    def test_plan_reuses_unchanged_source_and_rebuilds_only_changed_source(self) -> None:
        audit, rows, observed = baseline()
        plan = DELTA.make_plan(audit, "d" * 64, rows, observed, "e" * 40, 123,
                               "f" * 40, "1" * 64, "2" * 64, "3" * 64)
        self.assertEqual([item["action"] for item in plan["sources"]],
                         ["RETAIN", "REBUILD"])
        self.assertEqual(plan["sources"][1]["baseline_generation_seq"], 1)
        self.assertEqual(plan["sources"][1]["observed_watermark_sha256"], "c" * 64)
        self.assertEqual(plan["sources"][1]["application_read_bytes"], 24)
        observed[2]["application_read_bytes"] = None
        unmeasured = DELTA.make_plan(audit, "d" * 64, rows, observed, "e" * 40, 123,
                                     "f" * 40, "1" * 64, "2" * 64, "3" * 64)
        self.assertIsNone(unmeasured["sources"][1]["application_read_bytes"])
        rows[0]["active_generation_id"] = 11
        with self.assertRaisesRegex(RuntimeError, "pointer"):
            DELTA.make_plan(audit, "d" * 64, rows, observed, "e" * 40, 123,
                            "f" * 40, "1" * 64, "2" * 64, "3" * 64)

    def test_final_readback_binds_new_generation_and_qualification(self) -> None:
        audit, rows, observed = baseline()
        plan = DELTA.make_plan(audit, "d" * 64, rows, observed, "e" * 40, 123,
                               "f" * 40, "1" * 64, "2" * 64, "3" * 64)
        old = rows[1]["generations"][0]
        old["status"] = "verified"
        new = {"generation_id": 23, "generation_seq": 2,
               "status": "release_candidate", "commit_sha": "e" * 40,
               "item_count": 1,
               "source_watermark_sha256": "c" * 64,
               "adapter_profile_id": "fixture-adapter",
               "evidence_id": "fixture-new",
               "qualification_manifest_sha256": "f" * 64}
        rows[1]["generations"].append(new)
        receipt_set = {
            "schema_version": "mainrag.storage-v2.final-delta-receipts.v1",
            "sources": [{"source_id": 2, "artifact_path": "/protected/qualification.json",
                         "artifact_sha256": "1" * 64}],
        }
        verification = {key: "PASS" for key in (
            "artifact_root", "authorization", "body_pack_integrity", "intelligence",
            "intervals", "legacy_intelligence_export")}
        artifact = {
            "checkpoint": {"source_id": 2, "generation_id": 23,
                           "generation_seq": 2,
                           "commit_sha": "e" * 40,
                           "source_watermark_sha256": "c" * 64},
            "result": {"source_id": 2, "generation_id": 23,
                       "generation_seq": 2,
                       "status": "release_candidate", "evidence_id": "fixture-new",
                       "manifest_sha256": "f" * 64},
            "verification": {"source_id": 2, "generation_id": 23,
                             "generation_seq": 2,
                             "status": "verified", "checks": verification},
        }
        def read_artifact(path, digest):
            self.assertEqual(path, Path("/protected/qualification.json"))
            self.assertEqual(digest, "1" * 64)
            return artifact
        final = DELTA.finalize(plan, audit, receipt_set, rows, observed, read_artifact)
        self.assertEqual(final["schema_version"],
                         "mainrag.storage-v2.final-candidate-commit-map.v1")
        self.assertEqual(final["sources"], [
            {"source_id": 1, "candidate_generation_id": 11,
             "candidate_commit_sha": "b" * 40},
            {"source_id": 2, "candidate_generation_id": 23,
             "candidate_commit_sha": "e" * 40},
        ])
        rows[1]["generations"][1]["generation_seq"] = 3
        with self.assertRaisesRegex(RuntimeError, "qualification identity"):
            DELTA.finalize(plan, audit, receipt_set, rows, observed, read_artifact)
        rows[1]["generations"][1]["generation_seq"] = 2
        observed[2]["source_watermark_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "watermark drifted"):
            DELTA.finalize(plan, audit, receipt_set, rows, observed, read_artifact)


if __name__ == "__main__":
    unittest.main()
