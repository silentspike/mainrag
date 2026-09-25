"""Synthetic producer contract tests for managed append sources."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "managed_append.py"
SPEC = importlib.util.spec_from_file_location("mainrag_managed_append", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
managed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(managed)


class ManagedAppendProducerTests(unittest.TestCase):
    def test_append_is_ordered_and_restart_reads_the_same_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            first = managed.initialize(root)
            self.assertEqual(first["segments"], [])
            one = managed.append(root, b'{"event":"one"}\n')
            two = managed.append(root, b'{"event":"two"}\n')
            self.assertEqual(len(two["segments"]), 2)
            self.assertNotEqual(first["chain"], one["chain"])
            self.assertNotEqual(one["chain"], two["chain"])
            self.assertEqual(managed.load_manifest(root), two)
            self.assertEqual(
                [entry["sequence"] for entry in two["segments"]], [1, 2]
            )

    def test_shrink_prefix_drift_replacement_and_invalid_input_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            managed.initialize(root)
            baseline = managed.append(root, b'{"event":"one"}\n')
            segment = root / "segments" / baseline["segments"][0]["name"]
            segment.chmod(0o600)
            segment.write_bytes(b"different-length\n")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                managed.append(root, b'{"event":"two"}\n')
            segment.write_bytes(b'{"event":"one"}\n')

            path = root / "manifest.json"
            manifest = json.loads(path.read_text())
            manifest["segments"] = []
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "chain"):
                managed.load_manifest(root)
            path.write_text(json.dumps(baseline))
            segment.unlink()
            segment.symlink_to(root / "manifest.json")
            with self.assertRaisesRegex(ValueError, "missing or changed"):
                managed.load_manifest(root)

            for bad in (b"{}", b"[]\n", b"not-json\n", b"\n", b"x" * (managed.MAX_SEGMENT_BYTES + 1)):
                with self.subTest(bad=bad[:20]), self.assertRaises(ValueError):
                    managed.append(root, bad)

    def test_manifest_rotation_and_symlink_root_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            manifest = managed.initialize(root)
            changed = dict(manifest, epoch="00000000-0000-4000-8000-000000000001")
            (root / "manifest.json").write_text(json.dumps(changed))
            with self.assertRaisesRegex(ValueError, "chain"):
                managed.load_manifest(root)
            link = Path(temporary) / "linked"
            os.symlink(root, link)
            with self.assertRaisesRegex(ValueError, "real directory"):
                managed.load_manifest(link)


if __name__ == "__main__":
    unittest.main()
