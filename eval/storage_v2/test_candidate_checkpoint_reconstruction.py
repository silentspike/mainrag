"""Lost checkpoints retain original witnesses, adapter identity and restart proof."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "candidate_reconstruction", ROOT / "ops/storage-v2/reconstruct-candidate-checkpoint.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CandidateCheckpointReconstructionTests(unittest.TestCase):
    def test_candidate_reuse_preserves_build_identity_and_rejects_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = SimpleNamespace(database="fixture", source_id=1, generation_id=2,
                                   api_url="fixture", token_file=None, token_env="fixture",
                                   source_snapshot_review=None, pack_root=Path(temporary))
            for adapter, profile in (("fs", "mainrag.fs-release-candidate.v2.fixture"),
                                     ("pdf", "mainrag.pdf-release-candidate.v1.pdf-extract")):
                witness = {"kind":"release-candidate-build", "commit_sha":"a"*40,
                           "fixture_sha256":"b"*64, "source_watermark_sha256":"c"*64,
                           "adapter_profile_id":profile}
                generation = {"source_id":1, "generation_id":2, "generation_seq":3,
                              "status":"release_candidate", "run_status":"sealed",
                              "item_count":4, "run_expected_item_count":4,
                              "created_at_unix":100, "witness":witness,
                              "run_adapter_profile_id":profile, "source_type":adapter,
                              "active_generation_id":None}
                observed = {"source_id":1, "source_watermark_sha256":"c"*64,
                            "adapter_profile_id":profile, "item_count":4}
                state = {"active_generation_id":None, "server_instance_id":"restarted"}
                with patch.object(MODULE,"database_generation",return_value=generation), \
                        patch.object(MODULE,"load_token",return_value="fixture"), \
                        patch.object(MODULE,"api_service_start",return_value=(456,200)), \
                        patch.dict(MODULE.operator, request=lambda *a,**kw:observed,
                                   source_state=lambda *a,**kw:state,
                                   thin_pool_capacity=lambda *a,**kw:{"fixture":True}):
                    checkpoint=MODULE.reconstruct(args)
                    self.assertEqual(checkpoint["commit_sha"],witness["commit_sha"])
                    self.assertEqual(checkpoint["generation_id"],2)
                    self.assertEqual(checkpoint["build"]["fixture_sha256"],witness["fixture_sha256"])
                    self.assertEqual(checkpoint["reconstruction_evidence"]["generation_status"],
                                     "release_candidate")
                    observed["source_watermark_sha256"]="d"*64
                    with self.assertRaisesRegex(RuntimeError,"adapter observation differs"):
                        MODULE.reconstruct(args)
                    observed["source_watermark_sha256"]="c"*64
                    invalid=copy.deepcopy(generation)
                    invalid["run_adapter_profile_id"]="foreign-profile"
                    with patch.object(MODULE,"database_generation",return_value=invalid):
                        with self.assertRaisesRegex(RuntimeError,"witness is incomplete"):
                            MODULE.reconstruct(args)
                    with patch.object(MODULE,"api_service_start",return_value=(456,101)):
                        with self.assertRaisesRegex(RuntimeError,"restart after original build"):
                            MODULE.reconstruct(args)


if __name__ == "__main__":
    unittest.main()
