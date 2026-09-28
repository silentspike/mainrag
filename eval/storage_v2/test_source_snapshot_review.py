"""The source-drift review must observe bytes and one stable adapter snapshot."""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[2] / "ops/storage-v2/source-snapshot-review.py"
SPEC = importlib.util.spec_from_file_location("source_snapshot_review", PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SourceSnapshotReviewTests(unittest.TestCase):
    def test_registered_filter_is_profile_bound_and_excluded_files_are_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "session.jsonl").write_bytes(b"{}\n")
            proof = {"format": "mainrag.fs-scope.v1", "patterns": ["*.jsonl"],
                     "byte_regexes": [r"(?-u)^.*\.jsonl$"]}
            payload = MODULE.json.dumps([proof["patterns"], proof["byte_regexes"]],
                                        separators=(",", ":")).encode()
            proof["sha256"] = hashlib.sha256(b"mainrag.fs-scope.v1\0" + payload).hexdigest()
            profile = (f"mainrag.fs-release-candidate.v3.scope-{proof['sha256']}"
                       ".fragment-1048576-newline-65536")
            observation = {"source_id": 7, "source_watermark_sha256": "a" * 64,
                           "adapter_profile_id": profile, "item_count": 1,
                           "filesystem_scope": proof}
            includes = MODULE.registered_scope_matcher({"file_patterns": ["*.jsonl"]}, observation)
            self.assertTrue(includes("nested/naïve\nconversation.jsonl"))
            self.assertFalse(includes("session.jsonl\n"))
            registration = {"source_id": 7, "source_type": "fs", "source_path": str(root),
                            "config": '{"file_patterns":["*.jsonl"]}', "files": [
                {"path": "session.jsonl", "hash": hashlib.sha256(b"{}\n").hexdigest()},
                {"path": "private.json", "hash": "b" * 64}]}
            real_open = MODULE.os.open
            def guarded_open(path, *args, **kwargs):
                self.assertNotEqual(Path(path).name, "private.json")
                return real_open(path, *args, **kwargs)
            with patch.object(MODULE, "legacy_registration", return_value=registration), \
                    patch.object(MODULE, "observation", return_value=observation), \
                    patch.object(MODULE.os, "open", side_effect=guarded_open):
                review = MODULE.capture("fixture", "http://fixture.invalid", "token", 7)
            self.assertEqual(review["status_counts"], {"same_bytes": 1, "outside_configured_scope": 1})
            self.assertEqual(review["filesystem_scope"], proof)
            for bad in [{**observation, "adapter_profile_id": "old-unfiltered-adapter"},
                        {**observation, "filesystem_scope": None}]:
                with patch.object(MODULE, "legacy_registration", return_value=registration), \
                        patch.object(MODULE, "observation", return_value=bad):
                    with self.assertRaises(RuntimeError):
                        MODULE.capture("fixture", "http://fixture.invalid", "token", 7)
            registration["files"][1]["path"] = "../private.json"
            with patch.object(MODULE, "legacy_registration", return_value=registration), \
                    patch.object(MODULE, "observation", return_value=observation):
                with self.assertRaisesRegex(RuntimeError, "outside the registered source"):
                    MODULE.capture("fixture", "http://fixture.invalid", "token", 7)

    def test_git_review_uses_exact_clean_origin_and_rejects_checkout_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            root = cache / "fixture"
            root.mkdir(parents=True)
            def git(*args: str) -> None:
                subprocess.run(["git", "-C", str(root), *args], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git("init", "-b", "main")
            git("remote", "add", "origin", "https://fixture.invalid/fixture.git")
            (root / "same.txt").write_bytes(b"same")
            (root / "changed.txt").write_bytes(b"now")
            git("add", ".")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "-m", "fixture")
            registration = {"source_id": 7, "source_type": "git", "config": {},
                            "source_path": "https://fixture.invalid/fixture.git", "files": [
                {"path": "same.txt", "hash": hashlib.sha256(b"same").hexdigest()},
                {"path": "changed.txt", "hash": hashlib.sha256(b"old").hexdigest()},
                {"path": "missing.txt", "hash": hashlib.sha256(b"gone").hexdigest()}]}
            observation = {"source_id": 7, "source_watermark_sha256": "a" * 64,
                           "adapter_profile_id": "mainrag.git-release-candidate.v1", "item_count": 2}
            with patch.object(MODULE, "observation", return_value=observation), \
                    patch.object(MODULE, "legacy_registration", return_value=registration):
                review = MODULE.capture("fixture", "http://fixture.invalid", "token", 7, cache)
                self.assertEqual(review["source_type"], "git")
                self.assertEqual(len(review["git_head"]), 40)
                self.assertEqual(review["status_counts"], {
                    "same_bytes": 1, "changed_bytes": 1, "source_file_missing": 1})
                (root / "same.txt").write_bytes(b"dirty")
                with self.assertRaisesRegex(RuntimeError, "dirty"):
                    MODULE.capture("fixture", "http://fixture.invalid", "token", 7, cache)
                (root / "same.txt").write_bytes(b"same")
                git("remote", "set-url", "origin", "https://fixture.invalid/other.git")
                with self.assertRaisesRegex(RuntimeError, "differs"):
                    MODULE.capture("fixture", "http://fixture.invalid", "token", 7, cache)
                git("remote", "set-url", "origin", registration["source_path"])
                original = MODULE.git_checkout(registration, cache)
                with patch.object(MODULE, "git_checkout", side_effect=[original, (root, "b" * 40)]):
                    with self.assertRaisesRegex(RuntimeError, "checkout changed"):
                        MODULE.capture("fixture", "http://fixture.invalid", "token", 7, cache)

    def test_frozen_review_classifies_only_independently_read_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "same.txt").write_bytes(b"same")
            (root / "changed.txt").write_bytes(b"now")
            registration = {
                "source_id": 7, "source_type": "fs", "source_path": str(root),
                "config": {}, "files": [
                    {"path": "same.txt", "hash": hashlib.sha256(b"same").hexdigest()},
                    {"path": "changed.txt", "hash": hashlib.sha256(b"old").hexdigest()},
                    {"path": "missing.txt", "hash": hashlib.sha256(b"gone").hexdigest()},
                ],
            }
            observation = {"source_id": 7, "source_watermark_sha256": "a" * 64,
                           "adapter_profile_id": "fixture-adapter", "item_count": 2}
            with patch.object(MODULE, "observation", return_value=observation) as observed, \
                 patch.object(MODULE, "legacy_registration", return_value=registration):
                result = MODULE.capture("fixture", "http://fixture.invalid", "token", 7)
            self.assertEqual(observed.call_count, 2)
            self.assertEqual(result["status_counts"], {
                "same_bytes": 1, "changed_bytes": 1, "source_file_missing": 1})
            self.assertNotIn(str(root), str(result))
            self.assertNotIn("same.txt", str(result))
            changed = result["paths"][hashlib.sha256(b"changed.txt").hexdigest()]
            self.assertEqual(changed["observed_sha256"], hashlib.sha256(b"now").hexdigest())
            with patch.object(MODULE, "observation", side_effect=[
                observation, {**observation, "source_watermark_sha256": "b" * 64}
            ]), patch.object(MODULE, "legacy_registration", return_value=registration):
                with self.assertRaisesRegex(RuntimeError, "watermark changed"):
                    MODULE.capture("fixture", "http://fixture.invalid", "token", 7)
            registration["files"][0]["path"] = "../outside.txt"
            with patch.object(MODULE, "observation", return_value=observation), \
                 patch.object(MODULE, "legacy_registration", return_value=registration):
                with self.assertRaisesRegex(RuntimeError, "outside the registered source"):
                    MODULE.capture("fixture", "http://fixture.invalid", "token", 7)
