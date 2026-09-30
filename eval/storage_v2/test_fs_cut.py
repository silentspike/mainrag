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
    def test_snapshot_permission_scope_is_exact_and_only_selected_files_get_read_access(self):
        import types
        patterns = ["*.jsonl"]; regexes = [r"(?-u)^.*\.jsonl$"]
        payload = json.dumps([patterns,regexes],separators=(",", ":")).encode()
        proof = {"format":"mainrag.fs-scope.v1","patterns":patterns,"byte_regexes":regexes,
                 "sha256":hashlib.sha256(b"mainrag.fs-scope.v1\0"+payload).hexdigest()}
        matcher = PRODUCER["access_matcher"](proof)
        self.assertTrue(matcher("nested/private.jsonl"))
        self.assertFalse(matcher("credentials.json"))
        self.assertFalse(matcher("nested/private.jsonl\n"))
        for bad in ({**proof,"sha256":"a"*64},{**proof,"extra":True}):
            with self.assertRaises(RuntimeError):PRODUCER["access_matcher"](bad)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);nested=root/"private";nested.mkdir(mode=0o700)
            selected=nested/"history.jsonl";selected.write_bytes(b"fixture\n");selected.chmod(0o600)
            excluded=nested/"credentials.json";excluded.write_bytes(b"excluded");excluded.chmod(0o600)
            (nested/"linked.jsonl").symlink_to(excluded)
            commands=[]
            binary_meta=types.SimpleNamespace(st_mode=0o100755,st_uid=0)
            original_stat=Path.stat
            def metadata(path,*args,**kwargs):
                return binary_meta if str(path)=="/usr/bin/setfacl" else original_stat(path,*args,**kwargs)
            with patch.object(Path,"stat",metadata), \
                    patch.dict(PRODUCER["grant_snapshot_access"].__globals__,{
                        "trusted_directory":lambda *_:None,
                        "command":lambda binary,*args:commands.append(args)}):
                PRODUCER["grant_snapshot_access"](root,1234,matcher)
                file_commands=[cmd for cmd in commands if cmd[1]=="u:1234:r--"]
                self.assertEqual([str(p) for cmd in file_commands for p in cmd[3:]],[str(selected)])
                self.assertEqual(selected.read_bytes(),b"fixture\n")
                self.assertEqual(selected.stat().st_mode&0o777,0o600)
                self.assertEqual(excluded.stat().st_mode&0o777,0o600)
                os.link(selected,nested/"alias.jsonl")
                with self.assertRaisesRegex(RuntimeError,"hard-link"):
                    PRODUCER["grant_snapshot_access"](root,1234,matcher)

    def test_container_snapshot_preserves_pinned_boundary_and_rejects_unsafe_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent=Path(temporary)
            root,registry,view,history,descriptor,observation=self.fixture(parent)
            proof=observation["filesystem_cut"]["cut"]
            published=registry/"views"/proof["cut_id"]
            staged=published.with_suffix(".fixture-owned")
            published.rename(staged);published.mkdir(mode=0o750)
            staged.rename(published/"snapshot")
            descriptor=registry/"history"/f"{proof['cut_id']}-{proof['source_root_sha256']}.json"
            value=json.loads(descriptor.read_text());value["snapshot_root"]=str(published/"snapshot")
            value["read_root"]=str(published/"snapshot/sessions")
            raw=PRODUCER["canonical"](value);descriptor.write_bytes(raw)
            proof["descriptor_sha256"]=hashlib.sha256(raw).hexdigest()
            with patch.dict(CUT["read_root"].__globals__,{"inspect_kernel":lambda *_:None}):
                selected=CUT["read_root"](root,observation,registry,_owner=os.getuid())
                self.assertEqual(selected,published/"snapshot/sessions")
                self.assertEqual((selected/"live.jsonl").read_bytes(),b"first\n")
                published.chmod(0o777)
                with self.assertRaisesRegex(RuntimeError,"authority"):
                    CUT["read_root"](root,observation,registry,_owner=os.getuid())

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

    def test_conversation_projection_keeps_cut_and_filter_but_requires_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, registry, view, history, descriptor, observation = self.fixture(Path(temporary))
            original_profile = observation["adapter_profile_id"]
            observation["adapter_profile_id"] += ".text-utf8-nul-space-v1"
            config = {"filesystem_consistency": "btrfs-cut-v1",
                      "conversation_text_projection": "utf8-nul-space-v1"}
            self.assertTrue(SCOPE["registered_scope_matcher"](config, observation)("fixture.jsonl"))
            CUT["require_manifest"](observation["filesystem_cut"],
                observation["filesystem_cut"]["cut"]["source_root_sha256"],
                observation["adapter_profile_id"], observation["item_count"])
            with self.assertRaisesRegex(RuntimeError, "projection differs"):
                SCOPE["registered_scope_matcher"]({"filesystem_consistency":"btrfs-cut-v1"}, observation)
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                SCOPE["registered_scope_matcher"]({**config,"conversation_text_projection":"unknown"}, observation)
            observation["adapter_profile_id"] = original_profile
            with self.assertRaisesRegex(RuntimeError, "projection differs"):
                SCOPE["registered_scope_matcher"](config, observation)


if __name__ == "__main__":
    unittest.main()
