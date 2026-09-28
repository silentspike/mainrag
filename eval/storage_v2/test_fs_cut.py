"""Cut authority, pinned identity and independently bound manifest regressions."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CUT = runpy.run_path(str(ROOT / "ops/storage-v2/fs_cut.py"))
SCOPE = runpy.run_path(str(ROOT / "ops/storage-v2/fs_scope.py"))
PRODUCER = CUT["PRODUCER"]


def watermark(root, profile, fixture):
    digest = hashlib.sha256(b"mainrag.storage-v2.source-watermark.v1\0")
    for part in ("fs", str(root), profile, fixture):
        raw = part.encode(); digest.update(len(raw).to_bytes(8, "big")); digest.update(raw)
    return digest.hexdigest()


class CutTests(unittest.TestCase):
    def test_privileged_inspection_uses_only_opaque_bound_arguments(self):
        import types
        proof = {"source_root_sha256": "a" * 64, "cut_id": str(uuid.uuid4()),
                 "descriptor_sha256": "b" * 64, "snapshot_uuid": str(uuid.uuid4()),
                 "origin_uuid": str(uuid.uuid4())}
        expected = {"status": "PASS", "read_only": True, **proof}
        metadata = types.SimpleNamespace(st_uid=0, st_mode=0o100755)
        snapshot = Path("/synthetic/snapshot")
        with patch.dict(PRODUCER, {"trusted_directory": lambda *_: None}), \
                patch.object(Path, "stat", return_value=metadata), \
                patch.object(Path, "lstat", return_value=metadata), \
                patch.object(CUT["os"], "geteuid", return_value=1000), \
                patch.object(CUT["subprocess"], "run") as run:
            run.return_value.stdout = json.dumps(expected).encode()
            CUT["inspect_kernel"](snapshot, proof, 0)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/sudo", "-n",
                "/usr/libexec/mainrag/source-cut-capture", "--source-root-sha256", proof["source_root_sha256"],
                "--inspect-cut-id", proof["cut_id"]])
            for altered in ({**expected, "read_only": False}, {**expected, "snapshot_uuid": str(uuid.uuid4())},
                            {**expected, "descriptor_sha256": "c" * 64}, {**expected, "arbitrary_path": "/synthetic"}):
                run.return_value.stdout = json.dumps(altered).encode()
                with self.assertRaisesRegex(RuntimeError, "kernel"):
                    CUT["inspect_kernel"](snapshot, proof, 0)

    def fixture(self, parent):
        origin = parent / "origin"; root = origin / "sessions"
        registry = parent / "registry"; cut_id = str(uuid.uuid4())
        snapshot = registry / "views" / cut_id; view = snapshot / "sessions"
        for directory in (root, view, registry / "history"):
            directory.mkdir(parents=True, exist_ok=True)
        (root / "live.jsonl").write_bytes(b"first\n")
        (view / "live.jsonl").write_bytes(b"first\n")
        root_sha = hashlib.sha256(str(root).encode()).hexdigest()
        descriptor = {"format": "mainrag.fs-read-cut.v1", "cut_id": cut_id,
            "source_root_sha256": root_sha, "registered_root": str(root),
            "origin_subvolume": str(origin), "snapshot_root": str(snapshot), "read_root": str(view),
            "snapshot_uuid": str(uuid.uuid4()), "origin_uuid": str(uuid.uuid4()), "captured_at_unix": 1}
        raw = PRODUCER["canonical"](descriptor)
        history = registry / "history" / f"{cut_id}-{root_sha}.json"
        history.write_bytes(raw); history.chmod(0o600)
        proof = {key: descriptor[key] for key in (
            "format", "cut_id", "source_root_sha256", "snapshot_uuid", "origin_uuid", "captured_at_unix")}
        proof["descriptor_sha256"] = hashlib.sha256(raw).hexdigest()
        cut = {"cut": proof, "fixture_sha256": "a" * 64, "item_count": 1, "input_bytes": 6}
        profile = SCOPE["scope_profile"]("unfiltered", True)
        observation = {"source_id": 7, "adapter_profile_id": profile, "item_count": 1,
            "filesystem_cut": cut, "source_watermark_sha256": watermark(root, profile, cut["fixture_sha256"])}
        return root, registry, view, history, descriptor, observation

    def select(self, root, registry, observation, immutable=True):
        proof = observation["filesystem_cut"]["cut"]
        with patch.dict(PRODUCER, {
                "inspector": lambda: Path("/synthetic/btrfs"),
                "identity": lambda *_: {"uuid": proof["snapshot_uuid"], "parent_uuid": proof["origin_uuid"]},
                "command": lambda *_: "ro=true\n" if immutable else "ro=false\n"}):
            return CUT["read_root"](root, observation, registry, _owner=os.geteuid())

    def test_pinned_cut_survives_live_append_and_current_descriptor_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, registry, view, history, descriptor, observation = self.fixture(Path(temporary))
            (root / "live.jsonl").write_bytes(b"first\nsecond\n")
            (registry / "current-unrelated.json").write_text("unrelated")
            self.assertEqual(self.select(root, registry, observation), view)
            self.assertEqual((view / "live.jsonl").read_bytes(), b"first\n")
            self.assertTrue(CUT["cut_observation_valid"](observation["filesystem_cut"]))
            original=copy.deepcopy(observation["filesystem_cut"])
            equivalent=copy.deepcopy(original)
            equivalent["cut"].update(cut_id=str(uuid.uuid4()),snapshot_uuid=str(uuid.uuid4()),
                                      descriptor_sha256="e"*64,captured_at_unix=2)
            self.assertTrue(CUT["same_source_manifest"](original,equivalent))
            equivalent["input_bytes"]+=1
            self.assertFalse(CUT["same_source_manifest"](original,equivalent))
            altered = copy.deepcopy(observation); altered["filesystem_cut"]["fixture_sha256"] = "b" * 64
            with self.assertRaisesRegex(RuntimeError, "watermark"):
                self.select(root, registry, altered)
            self.assertNotEqual(str(view), str(root))

    def test_redirect_mutability_missing_history_and_unsafe_modes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, registry, view, history, descriptor, observation = self.fixture(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "kernel"):
                self.select(root, registry, observation, immutable=False)
            history.chmod(0o666)
            with self.assertRaisesRegex(RuntimeError, "authority"):
                self.select(root, registry, observation)
            history.chmod(0o600); original = history.read_bytes()
            descriptor["read_root"] = str(root)
            raw = PRODUCER["canonical"](descriptor); history.write_bytes(raw)
            observation["filesystem_cut"]["cut"]["descriptor_sha256"] = hashlib.sha256(raw).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "redirected"):
                self.select(root, registry, observation)
            history.unlink(); history.symlink_to(root / "live.jsonl")
            with self.assertRaises(OSError):
                self.select(root, registry, observation)
            history.unlink()
            with self.assertRaises(FileNotFoundError):
                self.select(root, registry, observation)

    def test_explicit_consistency_keeps_registered_filter_and_closed_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, registry, view, history, descriptor, observation = self.fixture(Path(temporary))
            includes = SCOPE["registered_scope_matcher"]({"filesystem_consistency": "btrfs-cut-v1"}, observation)
            self.assertTrue(includes("active.jsonl"))
            for config in ({}, {"filesystem_consistency": "unknown"}):
                with self.assertRaises(RuntimeError):
                    SCOPE["registered_scope_matcher"](config, observation)
            for field, value in (("item_count", True), ("input_bytes", -1), ("fixture_sha256", "wrong")):
                bad = copy.deepcopy(observation["filesystem_cut"]); bad[field] = value
                self.assertFalse(CUT["cut_observation_valid"](bad))
            bad = copy.deepcopy(observation["filesystem_cut"]); bad["cut"]["arbitrary_path"] = "/synthetic"
            self.assertFalse(CUT["cut_observation_valid"](bad))
            bad = copy.deepcopy(observation["filesystem_cut"]); bad["cut"]["snapshot_uuid"] = str(uuid.UUID(int=0))
            self.assertFalse(CUT["cut_observation_valid"](bad))


if __name__ == "__main__":
    unittest.main()
