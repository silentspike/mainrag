"""Focused checks for protected operator credential loading."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from operator_token import load_token


class OperatorTokenTests(unittest.TestCase):
    def test_owned_private_file_and_terminal_newline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_bytes(b"opaque.test-token\n")
            path.chmod(0o600)
            with patch.dict(os.environ, {"MAINRAG_TOKEN": "different"}):
                self.assertEqual(load_token(path, "MAINRAG_TOKEN"), "opaque.test-token")

    def test_environment_fallback(self) -> None:
        with patch.dict(os.environ, {"MAINRAG_TOKEN": "legacy-token"}):
            self.assertEqual(load_token(None, "MAINRAG_TOKEN"), "legacy-token")

    def test_rejects_symlinks_and_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_bytes(b"opaque-token")
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "owned private regular file"):
                load_token(path, "MAINRAG_TOKEN")
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                load_token(link, "MAINRAG_TOKEN")

    def test_rejects_empty_and_whitespace_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.touch()
            path.chmod(0o600)
            for content in (b"", b"bad token", b"bad\ntoken"):
                path.write_bytes(content)
                with self.assertRaises(RuntimeError):
                    load_token(path, "MAINRAG_TOKEN")


if __name__ == "__main__":
    unittest.main()
