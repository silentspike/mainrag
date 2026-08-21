from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from storage_v2.prepare_shadow_fixture import apply_delta, prepare, reset_to_base, verify


class PrepareShadowFixtureTests(unittest.TestCase):
    def test_prepare_verify_and_delta_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "source"
            base = prepare(output)
            self.assertEqual(verify(output), base)
            self.assertEqual(len(base["files"]), 13)

            delta = apply_delta(output)
            self.assertEqual(verify(output), delta)
            self.assertNotEqual(base["fixture_sha256"], delta["fixture_sha256"])

            restored = reset_to_base(output)
            self.assertEqual(verify(output), restored)
            self.assertEqual(restored, base)

    def test_reset_requires_the_verified_delta_variant(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "source"
            prepare(output)
            with self.assertRaisesRegex(RuntimeError, "variant"):
                reset_to_base(output)

    def test_prepare_refuses_to_replace_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "source"
            output.mkdir()
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                prepare(output)

    def test_unmanifested_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "source"
            prepare(output)
            (output / "fixture_symbol.rs").write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "differ"):
                verify(output)


if __name__ == "__main__":
    unittest.main()
