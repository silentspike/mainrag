"""Sealed original projections survive Unicode planning and unknown commits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest

M = runpy.run_path(str(Path(__file__).resolve().parents[2] / "ops/storage-v2/restore-candidate-projections.py"))


class ProjectionPlannerTests(unittest.TestCase):
    def test_unicode_repetition_boundaries_and_sql_text_delimiters(self):
        text = ("naïve 日本語 🧪\n" * 180) + "$projection_guard$'; SELECT 1; --"
        rows = M["canonical_segments"](text)
        self.assertGreater(len(rows), 2)
        for order, piece, position in rows:
            self.assertEqual(order, rows.index((order, piece, position)))
            self.assertEqual(piece, text[position - 1:position - 1 + len(piece)])
            self.assertEqual(position, text.find(piece) + 1)
            self.assertLessEqual(len(piece), 1000)
        self.assertEqual(M["canonical_segments"](""), [])
        self.assertEqual(M["canonical_segments"]("a" * 1000), [(0, "a" * 1000, 1)])
        self.assertEqual(len(M["canonical_segments"]("a" * 1001)), 2)
        with self.assertRaises(RuntimeError):
            M["canonical_segments"]("x" * (M["MAX_DOCUMENT_BYTES"] + 1))
        with self.assertRaises(RuntimeError):
            M["literal"]("zero\x00byte")

    def test_private_identity_and_preflight_are_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            file = root / "proof.json"
            file.write_text(json.dumps({"schema_version": "mainrag-storage-v2-preflight/v1",
                                       "mode": "check", "overall_status": "PASS", "checks": dict.fromkeys(M["PREFLIGHT_CHECKS"], "PASS")}))
            file.chmod(0o600)
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            now = int(file.stat().st_mtime)
            self.assertEqual(M["verify_preflight"](file, sha, now)["overall_status"], "PASS")
            for bad_sha, bad_now in [("a" * 64, now), (sha, now + 901)]:
                with self.assertRaises(RuntimeError):
                    M["verify_preflight"](file, bad_sha, bad_now)
            file.chmod(0o644)
            with self.assertRaises(RuntimeError):
                M["private_json"](file, sha)
